"""The process that keeps ``code_build``'s promise.

``jarvis/tools/builtin/code_build.py`` files one ``repo_setup`` job carrying the
user's own words and says "I'll read the requirements back to you before anything
is created". For one release nothing kept that promise: the row sat queued
forever. This is the state machine that does.

    queued      tidy the transcript, read the list back, wait
    readback    they approved -> name the repository, wait
    confirm     they approved -> create it, clone it, start the build
    done        a claude_code job exists with a cwd, and stage 2 runs in it

EVERY STEP IS A ROW, because the runner may die between any two of them. There is
no in-memory state and no resumption token: :func:`advance` reads the job and the
request it is parked on, works out where it is from those two facts alone, and
does the next thing. Called twice, it does the next thing once — the answer it
acts on is CONSUMED, and a consumed answer cannot authorise a second action.

THE MODEL IS INJECTED AND SO IS GITHUB. This module may not import
:mod:`jarvis.live` (that would put google-genai on the import path of a runner
with no key) nor any channel (the read-back is a row, and which channel presents
it is decided by presence, hours later, in another process). ``Deps`` is the
whole of what it needs from the outside, passed every call.

THE READ-BACK IS NOT PARAPHRASED. The requirement list goes into the
presentation's ``intro`` with ``verbatim=True``, so a channel with a
deterministic reader speaks it word for word and
:class:`jarvis.voice.router.OutputRouter` refuses to route it to the
conversational voice. That is the whole of R2's fidelity guarantee at the point
where the user says yes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jarvis import jobs, spec
from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.github.scopes import Capabilities
from jarvis.github.transport import Transport
from jarvis.ids import dedupe_key
from jarvis.project import lifecycle as lc
from jarvis.project.workspace import Git

__all__ = [
    "APPROVE_LABEL",
    "BUILD_JOB_KIND",
    "CHANGE_LABEL",
    "READBACK_QUESTION",
    "Deps",
    "Step",
    "advance",
    "readback_presentation",
]

BUILD_JOB_KIND = "repo_setup"

READBACK_QUESTION = "Shall I build that?"
APPROVE_LABEL = "Build it"
CHANGE_LABEL = "Change something"

#: What the user may say instead of picking. Spoken after the options, so
#: "drop three" is offered rather than discovered.
EDIT_PROMPT = "Or say what to change — 'drop three', 'two should say Postgres', 'add: no Docker'."


class BuildRefused(RuntimeError):
    """The build cannot go on, and the message is the sentence to say."""


@dataclass(frozen=True, slots=True)
class Deps:
    """Everything the runner needs from outside its own layer. Passed every call.

    ``git_token`` has no default. ``prepare_workspace`` clones a PRIVATE
    repository over HTTPS with ``GIT_TERMINAL_PROMPT=0``, so a missing token is
    not "unauthenticated, might work" — it is an instant failure whose message
    mentions neither the token nor the repository.
    """

    model_call: spec.ModelCall
    git: Git
    git_token: str | None
    capabilities: Capabilities
    owner: str = ""
    workspace_root: Path = Path("~/code")
    transport: Transport | None = None
    actor: str = "builder"

    @property
    def can_make_a_repo(self) -> bool:
        """Whether this install can do R2's repo-first half at all.

        False is a supported configuration, not a degraded one: with no token
        the build still happens, in a local directory, and the runner says so
        rather than failing. What it must never do is stay quiet about it.
        """
        return (
            bool(self.owner)
            and self.transport is not None
            and self.capabilities.of("create") != "no"
        )

    def project_config(self) -> lc.ProjectConfig:
        return lc.ProjectConfig(
            owner=self.owner, capabilities=self.capabilities, workspace_root=self.workspace_root
        )


Action = Literal["tidied", "waiting", "building", "refused", "nothing"]


@dataclass(frozen=True, slots=True)
class Step:
    """What one call to :func:`advance` did. ``spoken`` is always sayable as it stands."""

    action: Action
    spoken: str
    job_id: str
    request_id: str | None = None
    child_job_id: str | None = None
    cwd: str | None = None
    requirements: tuple[str, ...] = field(default=())

    @property
    def done(self) -> bool:
        return self.action in ("building", "refused")


# ───────────────────────────── the read-back ─────────────────────────────


def readback_presentation(sp: spec.Spec, transcript: str) -> rq.Presentation:
    """The numbered requirements, plus every mechanical flag, as one Presentation.

    The coverage sentence comes FIRST and is unconditional when anything was
    flagged: "I may have missed 'no auth'" is worth two seconds of speech, and a
    silently dropped negation costs a rebuild. Everything here is composed by
    :mod:`jarvis.spec` next to the checks that produced it, so this function adds
    no prose of its own beyond the numbering.
    """
    lines: list[str] = []
    if note := spec.coverage_sentence(spec.audit(transcript, sp)):
        lines.append(note)
    lines.append("Here's what I have.")
    lines += [f"{n}. {text}" for n, text in spec.readback_items(sp)]
    for note in (spec.effort_note(sp), spec.mode_note(sp)):
        if note:
            lines.append(note)
    return rq.make_presentation(
        intro="\n".join(lines),
        options=[
            {"label": APPROVE_LABEL, "description": "start building exactly that"},
            {"label": CHANGE_LABEL, "description": "the list is not right yet"},
        ],
        verbatim=True,
        allows_free_text=True,
        free_text_prompt=EDIT_PROMPT,
        dtmf_map={"1": 1, "2": 2},
        question=READBACK_QUESTION,
    )


def _payload(sp: spec.Spec, transcript: str) -> dict[str, Any]:
    """The row's self-description, and the only surviving record of what was agreed.

    The requirement list is stored with its ids and its source spans, so an edit
    arriving hours later on another channel is applied to the same list the user
    heard rather than to a re-tidy — which would be a fresh chance to drift.
    """
    return {
        "questions": [
            {
                "question": READBACK_QUESTION,
                "header": "the build",
                "options": [
                    {"label": APPROVE_LABEL, "description": "start building exactly that"},
                    {"label": CHANGE_LABEL, "description": "the list is not right yet"},
                ],
            }
        ],
        "transcript": transcript,
        "repo_name": sp.repo_name,
        "revision": sp.revision,
        "next_id": sp.next_id,
        "requirements": [
            {"id": r.id, "text": r.text, "quote": r.quote, "origin": r.origin}
            for r in sp.requirements
        ],
    }


def _spec_from(payload: dict[str, Any]) -> tuple[spec.Spec, str]:
    """Rebuild the Spec the user heard, and the transcript it came from.

    The three ``*_ask`` fields are re-derived from the transcript rather than
    stored: they are pure functions of the user's own words
    (:func:`jarvis.spec.parse_model` and friends), so deriving them cannot
    disagree with what was said, while a stored copy could go stale against an
    edited list.
    """
    transcript = str(payload.get("transcript") or "")
    reqs = tuple(
        spec.Requirement(
            id=int(r["id"]),
            text=str(r["text"]),
            quote=r.get("quote"),
            origin=r.get("origin", "transcript"),
        )
        for r in payload.get("requirements") or ()
    )
    return (
        spec.Spec(
            requirements=reqs,
            mode="local",
            model=spec.parse_model(transcript).alias,
            effort=spec.parse_effort(transcript).level,
            repo_name=str(payload.get("repo_name") or ""),
            model_ask=spec.parse_model(transcript),
            effort_ask=spec.parse_effort(transcript),
            mode_ask=spec.parse_mode(transcript),
            revision=int(payload.get("revision") or 0),
            next_id=int(payload.get("next_id") or (len(reqs) + 1)),
        ),
        transcript,
    )


def _raise_readback(
    con: sqlite3.Connection, job: jobs.Job, sp: spec.Spec, transcript: str, deps: Deps, attempt: int
) -> rq.Request:
    req = rq.create_request(
        con,
        dedupe_key=dedupe_key(job.id, "project.readback", sp.revision),
        attempt=attempt,
        kind="readback",
        short_label="the requirements",
        presentation=readback_presentation(sp, transcript),
        payload=_payload(sp, transcript),
        actor=deps.actor,
        job_id=job.id,
    )
    # DEFERRED, not blocked: this process is about to exit. `blocked` means a
    # runner is sitting on the row, and reconcile treats a blocked job with no
    # live process as an orphan to respawn.
    jobs.mark_blocked(con, job.id, req.id, actor=deps.actor, state="deferred")
    return req


# ───────────────────────────── the machine ─────────────────────────────


def advance(con: sqlite3.Connection, job_id: str, deps: Deps, *, now_ts: str | None = None) -> Step:
    """Do the next thing for one build, and stop. Safe to call again at any time."""
    job = jobs.get(con, job_id)
    if job is None:
        raise BuildRefused(f"no job {job_id}")
    if job.kind != BUILD_JOB_KIND:
        raise BuildRefused(f"{job_id} is a {job.kind} job, not a build request")
    if job.terminal:
        return Step("nothing", "That build is already over.", job_id)

    if job.state == "queued":
        return _tidy(con, job, deps)

    req = rq.get_request(con, job.blocked_request_id) if job.blocked_request_id else None
    if req is None:
        return Step("nothing", "That build is not waiting on anything I can see.", job_id)
    if req.state == "pending":
        return Step("nothing", "Still waiting on you.", job_id, request_id=req.id)

    if req.kind == "readback":
        return _after_readback(con, job, req, deps)
    return _after_name(con, job, req, deps)


def _tidy(con: sqlite3.Connection, job: jobs.Job, deps: Deps) -> Step:
    """Turn the stored transcript into a list, and ask about it."""
    transcript = (job.prompt_text or "").strip()
    if not transcript:
        jobs.set_state(con, job.id, "failed", actor=deps.actor, stop_reason="no transcript")
        return Step("refused", "I don't have your words for that build any more.", job.id)

    jobs.set_state(con, job.id, "starting", actor=deps.actor)
    jobs.set_state(con, job.id, "running", actor=deps.actor)
    try:
        sp = spec.tidy(transcript, deps.model_call)
    except Exception as exc:  # noqa: BLE001 - a dead tidier must not eat the request
        # 'parked', not 'failed': the transcript is still on the row and a retry
        # costs one model call, so a network blip must not destroy the request.
        jobs.set_state(con, job.id, "parked", actor=deps.actor, stop_reason=f"tidy: {exc}")
        return Step("refused", f"I couldn't tidy that up just now — {exc}", job.id)

    if not sp.requirements:
        jobs.set_state(con, job.id, "parked", actor=deps.actor, stop_reason="nothing traceable")
        return Step(
            "refused",
            "I couldn't trace a single requirement back to your own words, so I have not "
            "guessed at any. Tell me again, with what it should do?",
            job.id,
        )

    req = _raise_readback(con, job, sp, transcript, deps, attempt=1)
    return Step(
        "waiting",
        str(req.presentation["intro"]),
        job.id,
        request_id=req.id,
        requirements=tuple(text for _, text in spec.readback_items(sp)),
    )


def _after_readback(con: sqlite3.Connection, job: jobs.Job, req: rq.Request, deps: Deps) -> Step:
    """They answered the read-back: approve, change, or an edit in their own words."""
    answer = rq.consume(con, req.id, actor=deps.actor) or {}
    sp, transcript = _spec_from(req.payload)
    said = str(answer.get("text") or "").strip()
    picked = _picked(answer)

    if said and picked is None:
        edit = spec.parse_edit(said)
        if edit is None:
            req2 = _raise_readback(con, job, sp, transcript, deps, attempt=req.attempt + 1)
            return Step(
                "waiting",
                f"I didn't follow '{said}'. {req2.presentation['intro']}",
                job.id,
                request_id=req2.id,
            )
        try:
            sp = spec.apply_edit(sp, edit, expect_revision=sp.revision)
        except (spec.EditIndexError, spec.StaleSpec) as exc:
            req2 = _raise_readback(con, job, sp, transcript, deps, attempt=req.attempt + 1)
            return Step(
                "waiting", f"{exc} {req2.presentation['intro']}", job.id, request_id=req2.id
            )
        req2 = _raise_readback(con, job, sp, transcript, deps, attempt=req.attempt + 1)
        return Step(
            "waiting",
            str(req2.presentation["intro"]),
            job.id,
            request_id=req2.id,
            requirements=tuple(t for _, t in spec.readback_items(sp)),
        )

    if picked != APPROVE_LABEL:
        req2 = _raise_readback(con, job, sp, transcript, deps, attempt=req.attempt + 1)
        return Step(
            "waiting",
            "What should change? " + EDIT_PROMPT,
            job.id,
            request_id=req2.id,
        )

    _resume(con, job, deps)
    if not deps.can_make_a_repo:
        # No token, no owner, or a token that cannot create: build LOCALLY and
        # say so. R2's order is "the repository before the code" — it is not
        # "refuse to work without one", and a silent local build would be the
        # dishonesty this project exists to avoid.
        return _start_build(con, job, sp, transcript, deps, name=sp.repo_name, repo=None)

    proposal = lc.propose(
        con,
        config=deps.project_config(),
        spoken_name=sp.repo_name.replace("-", " "),
        transport=deps.transport,  # type: ignore[arg-type]
        actor=deps.actor,
        job_id=job.id,
    )
    if proposal.request_id is None:
        return _start_build(con, job, sp, transcript, deps, name=sp.repo_name, repo=None)
    jobs.mark_blocked(con, job.id, proposal.request_id, actor=deps.actor, state="deferred")
    return Step("waiting", proposal.spoken, job.id, request_id=proposal.request_id)


def _after_name(con: sqlite3.Connection, job: jobs.Job, req: rq.Request, deps: Deps) -> Step:
    """They answered the repository-name question. Create it, clone it, build."""
    made = lc.create(
        con,
        config=deps.project_config(),
        request_id=req.id,
        transport=deps.transport,  # type: ignore[arg-type]
        actor=deps.actor,
        job_id=job.id,
    )
    if not made.ok or made.name is None:
        if made.request_id and made.request_id != req.id:
            jobs.mark_blocked(con, job.id, made.request_id, actor=deps.actor, state="deferred")
            return Step("waiting", made.spoken, job.id, request_id=made.request_id)
        _resume(con, job, deps)
        jobs.set_state(con, job.id, "parked", actor=deps.actor, stop_reason=made.state)
        return Step("refused", made.spoken, job.id)

    _resume(con, job, deps)
    readback = _newest_readback(con, job.id)
    sp, transcript = _spec_from(readback.payload if readback else {})
    return _start_build(con, job, sp, transcript, deps, name=made.name, repo=made.full_name)


def _start_build(
    con: sqlite3.Connection,
    job: jobs.Job,
    sp: spec.Spec,
    transcript: str,
    deps: Deps,
    *,
    name: str,
    repo: str | None,
) -> Step:
    """Clone if there is something to clone, then hand stage 2 a job with a cwd."""
    root = deps.workspace_root.expanduser()
    if repo is not None:
        ws = lc.prepare_workspace(
            config=deps.project_config(),
            name=name,
            git=deps.git,
            token=deps.git_token,
        )
        if not ws.ok:
            # PARKED, never failed: a half-written clone from a crash, or a
            # directory the user moved, is recoverable by hand — and 'failed' is
            # terminal, so it would strand the build with no path back.
            jobs.set_state(con, job.id, "parked", actor=deps.actor, stop_reason=ws.state)
            return Step("refused", ws.spoken, job.id)
        cwd = ws.path
    else:
        cwd = root / name
        cwd.mkdir(parents=True, exist_ok=True)

    child = jobs.create_job(
        con,
        kind="claude_code",
        title=job.title,
        created_by=deps.actor,
        actor=deps.actor,
        cwd=str(cwd),
        repo=repo,
        model=job.model,
        effort=job.effort,
        prompt_text=spec.assemble_prompt(sp, transcript),
        kill_epoch=job.kill_epoch,
    )
    publish(
        con,
        "project.started",
        deps.actor,
        {"child_job_id": child.id, "repo": repo, "cwd": str(cwd), "local_only": repo is None},
        job_id=job.id,
        idem_key=f"project:{job.id}:started",
    )
    jobs.set_state(con, job.id, "finishing", actor=deps.actor)
    jobs.set_state(con, job.id, "done", actor=deps.actor, result_summary=f"build started in {cwd}")

    where = f"{repo}, cloned to {cwd}" if repo else f"{cwd} — no repository, I don't have a token"
    return Step(
        "building",
        f"Starting it now in {where}.",
        job.id,
        child_job_id=child.id,
        cwd=str(cwd),
        requirements=tuple(t for _, t in spec.readback_items(sp)),
    )


def _resume(con: sqlite3.Connection, job: jobs.Job, deps: Deps) -> None:
    """Come back from ``deferred`` the long way, because the short way is illegal.

    ``deferred -> running`` is not a legal transition and ``unblock`` defaults to
    it: from ``deferred`` a job may only go to ``starting`` (or to a terminal
    state). That is the state machine refusing to let a parked job pretend it
    never stopped — every resume passes through ``starting``, which is the state
    ``reconcile`` gives a grace period to.
    """
    jobs.unblock(con, job.id, state="starting", actor=deps.actor)
    jobs.set_state(con, job.id, "running", actor=deps.actor)


def _picked(answer: dict[str, Any]) -> str | None:
    """The LABEL they chose, or None when they said something of their own.

    ``answers`` looks identical either way — it is keyed by question and holds a
    string — so the two are told apart by ``sources``, which is the field the
    channels set for exactly this. Without it "drop three" arrives as a picked
    option whose label happens to be "drop three", the edit is never applied, and
    the user is read back the list they just asked to change.
    """
    sources = answer.get("sources") or {}
    for question, value in (answer.get("answers") or {}).items():
        if sources.get(question) == "free_text":
            continue
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value:
            return str(value[0])
    return None


def _newest_readback(con: sqlite3.Connection, job_id: str) -> rq.Request | None:
    """The list the user actually approved, which by now is consumed.

    Read back out of the row rather than carried in memory: the process that
    raised it may be long dead, and the answered request is the only surviving
    record of what was agreed.
    """
    found = rq.requests_for_job(con, job_id, kind="readback", limit=1)
    return found[0] if found else None
