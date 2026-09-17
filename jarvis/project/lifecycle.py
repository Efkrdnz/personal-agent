"""Spoken name -> confirmed slug -> repository -> clone -> a job with a cwd.

This is the order requirement R2 actually asks for and stage 2 skipped: the
repository FIRST, private and empty, and only then code in it. Every step is a
row another process can read after this one died, and every refusal has a
sentence:

    propose()            slug it, check the name, raise the read-back
    confirm.decision()   what the human said
    create()             consume the yes, enqueue ONE at-most-once row, run it
    prepare_workspace()  clone or refuse, with the caller's token
    start_build_job()    the jobs row with cwd set, so stage 2 runs inside it unchanged
    undo_repo()          run whatever the token has, and say what it could not do

TWO EFFECT ROWS, AND THE REASON IS THE WHOLE STAGE. ``github.repo_create`` is
classified ``irreversible`` in the spine's frozen table because the token has no
``delete_repo`` scope, and :func:`jarvis.effects.record_effect` therefore refuses
to record it with an undo plan — correctly, because a plan for something that
cannot be undone is a promise waiting to be spoken. But "this repository is LIVE
UNDER THIS NAME" *can* be answered: renamed to ``zz-abandoned-<slug>``, made
private, archived. Those are two different facts with two different classes, so
they are two rows:

``github.repo_create``  irreversible, no plan. It exists and always will. Asked
                        to undo it, Jarvis says exactly that, with the reason.
``github.repo_live``    compensatable, carrying the declarative plan. This is the
                        row "undo that" acts on, and it is written ONLY when a
                        compensation can actually be attempted.

When the matrix says the token can do nothing, the second row is not written at
all: "undo that" then finds the irreversible row and the wording becomes "there
is nothing I can do about it now", which is the roadmap's own exit condition.

NOTHING HERE HOLDS A CREDENTIAL. The GitHub transport and the git token are
parameters, every time, so a test cannot reach a real account even by accident.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from jarvis import effects as fx
from jarvis import jobs
from jarvis.bus import publish
from jarvis.github import repos as gh
from jarvis.github.scopes import Capabilities
from jarvis.github.transport import GithubError, Transport, TransportError
from jarvis.project import compensate, confirm, outbox
from jarvis.project.slug import SlugError, slugify
from jarvis.project.workspace import Git, Workspace, clone_url
from jarvis.project.workspace import prepare as prepare_clone
from jarvis.requests import consume

__all__ = [
    "REPO_CREATE_KIND",
    "REPO_LIVE_KIND",
    "Creation",
    "ProjectConfig",
    "Proposal",
    "create",
    "prepare_workspace",
    "propose",
    "run_repo_create",
    "start_build_job",
    "undo_repo",
    "undoable_repo_effect",
]

#: The spine's own kind for the irreversible half, named here so a reader can see
#: which row is which without going to look.
REPO_CREATE_KIND = "github.repo_create"

#: The compensatable half: this repository is the live one under this name.
REPO_LIVE_KIND = "github.repo_live"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Everything about this install that a second install would get wrong.

    ``capabilities`` has no default. An install that has not read its token's
    matrix is exactly the install whose confirmation would over-promise, and
    :func:`jarvis.github.scopes.capabilities` reads it with one request that
    changes nothing — so there is no honest default to offer.

    ``workspace_root`` is stored unexpanded so the value in ``config.toml`` is the
    value in the row; :func:`jarvis.project.workspace.prepare` expands it once, at
    the moment it touches the disk.
    """

    owner: str
    capabilities: Capabilities
    workspace_root: Path = Path("~/projects")
    host: Literal["local"] = "local"

    def __post_init__(self) -> None:
        if not str(self.owner).strip():
            raise ValueError("owner is required: a repository has to be created under somebody")
        if self.host != "local":
            # ADR 0009. The classifier still ships and still records the phrasing;
            # what it must never do is quietly point a job at a machine where a
            # deferred permission is converted to a hard deny.
            raise ValueError("cloud mode is cut from v1 (ADR 0009); host must be 'local'")

    @classmethod
    def from_mapping(cls, m: dict[str, Any], *, capabilities: Capabilities) -> ProjectConfig:
        """Build from the ``[project]`` table of config.toml, ignoring strangers.

        The matrix is passed separately and never read from the file: it is
        MEASURED from the credential in use, and a hand-edited copy of it would be
        the one thing in this system that could make the spoken line lie without
        anybody touching the code.
        """
        fields = set(cls.__dataclass_fields__) - {"capabilities"}
        kw: dict[str, Any] = {k: v for k, v in m.items() if k in fields}
        if "workspace_root" in kw:
            kw["workspace_root"] = Path(str(kw["workspace_root"]))
        return cls(capabilities=capabilities, **kw)


ProposalState = Literal["confirming", "name_taken", "cannot_name", "cannot_check", "cannot_create"]


@dataclass(frozen=True, slots=True)
class Proposal:
    """What came of turning spoken words into a repository name.

    ``request_id`` is the row a human answers next — the read-back for
    ``confirming``, the rename question for ``name_taken`` — and is None for the
    outcomes where there is nothing to answer.
    """

    state: ProposalState
    owner: str
    spoken: str
    name: str | None = None
    spoken_name: str | None = None
    request_id: str | None = None

    @property
    def full_name(self) -> str | None:
        return f"{self.owner}/{self.name}" if self.name else None


CreationState = Literal[
    "created",
    "collision",
    "refused",
    "needs_human",
    "not_confirmed",
    "busy",
    "cannot_create",
]


@dataclass(frozen=True, slots=True)
class Creation:
    """What came of trying to create it. ``spoken`` is always sayable as it stands."""

    state: CreationState
    owner: str
    spoken: str
    name: str | None = None
    outbox_id: str | None = None
    create_effect_id: str | None = None
    live_effect_id: str | None = None
    repo: gh.Repo | None = None
    request_id: str | None = None
    decision: confirm.Decision | None = None

    @property
    def ok(self) -> bool:
        return self.state == "created"

    @property
    def full_name(self) -> str | None:
        return f"{self.owner}/{self.name}" if self.name else None


# ───────────────────────────── propose ─────────────────────────────


def propose(
    con: sqlite3.Connection,
    *,
    config: ProjectConfig,
    spoken_name: str,
    transport: Transport,
    actor: str,
    job_id: str | None = None,
) -> Proposal:
    """Slug the name, check it is free, and raise the question that fits.

    Nothing is created here and nothing can be: the only writes are a ``requests``
    row and its bus event. The existence check runs BEFORE the read-back so the
    user is never asked to confirm a name that was already taken while they were
    listening to it — and it is only the cheap half of the collision check, since
    GitHub answers 404 for a private repository this token may not see. The other
    half is the create's own 422, handled in :func:`run_repo_create`.
    """
    owner = config.owner
    caps = config.capabilities
    if caps.of("create") == "no":
        return Proposal(
            "cannot_create",
            owner,
            "This token can't create repositories, so I can't start that one. "
            f"{_why_not_create(caps)}",
            spoken_name=spoken_name,
        )

    try:
        name = slugify(spoken_name)
    except SlugError as exc:
        return Proposal("cannot_name", owner, exc.spoken, spoken_name=spoken_name)

    fn = f"{owner}/{name}"
    try:
        taken = gh.exists(transport, owner, name)
    except TransportError as exc:
        return Proposal(
            "cannot_check",
            owner,
            f"I couldn't reach GitHub to check whether {fn} is free, so I haven't created "
            f"anything. Ask me again in a moment. ({exc})",
            name=name,
            spoken_name=spoken_name,
        )
    except GithubError as exc:
        return Proposal(
            "cannot_check",
            owner,
            f"GitHub wouldn't tell me whether {fn} exists — {exc.status} {exc.message} — "
            "so I haven't created anything.",
            name=name,
            spoken_name=spoken_name,
        )

    if taken:
        reason = (
            f"There is already a repository called {name} under {owner}, and I am not going to "
            "add a number to the end of it."
        )
        req = confirm.raise_rename(
            con, owner=owner, rejected=name, reason=reason, actor=actor, job_id=job_id
        )
        return Proposal(
            "name_taken",
            owner,
            f"{reason} {confirm.RENAME_QUESTION}",
            name=name,
            spoken_name=spoken_name,
            request_id=req.id,
        )

    req = confirm.raise_confirmation(
        con,
        owner=owner,
        name=name,
        spoken_name=spoken_name,
        caps=caps,
        actor=actor,
        job_id=job_id,
    )
    return Proposal(
        "confirming",
        owner,
        str(req.presentation["intro"]),
        name=name,
        spoken_name=spoken_name,
        request_id=req.id,
    )


def _why_not_create(caps: Capabilities) -> str:
    """The measured reason, so "I can't" is never a flat no.

    Built from the matrix's own fields rather than from its prose: the notes are
    written for a reader of the whole matrix, and the one that happens to be first
    is about deletion.
    """
    if caps.scopes is not None:
        held = ", ".join(caps.scopes) or "no scopes at all"
        return f"It holds {held}, which cannot create the private repository this needs."
    return f"The credential is a {caps.token_kind} token and does not report what it can do."


# ───────────────────────────── create ─────────────────────────────


def create(
    con: sqlite3.Connection,
    *,
    config: ProjectConfig,
    request_id: str,
    transport: Transport,
    actor: str,
    job_id: str | None = None,
    claimed_by: str | None = None,
) -> Creation:
    """Create the repository the human said yes to, and nothing else.

    The answered request is the ONLY authorisation. Still pending, denied, lapsed
    into a denial, or answered with a different name: all of them produce
    ``not_confirmed`` and write no outbox row at all.

    The yes is CONSUMED before the call goes out, so one answer can never
    authorise two creations. A resumed process that runs this again meets two
    independent refusals: a consumed request, and an outbox row that has already
    been attempted.
    """
    decided = confirm.decision(con, request_id)
    if decided.kind != "create":
        # One state, with the Decision attached: "still waiting", "they said no"
        # and "they said a different name" are all NOT an authorisation, and a
        # caller that needs to tell them apart reads `decision` rather than
        # matching on a second enum that could disagree with the first.
        return Creation(
            "not_confirmed",
            config.owner,
            decided.spoken,
            name=decided.name,
            request_id=request_id,
            decision=decided,
        )
    name = decided.name
    if not name:
        return Creation(
            "not_confirmed",
            config.owner,
            "I couldn't tell which name that yes was for, so I created nothing.",
            request_id=request_id,
            decision=decided,
        )
    if config.capabilities.of("create") == "no":
        return Creation(
            "cannot_create",
            config.owner,
            f"This token can't create repositories. {_why_not_create(config.capabilities)}",
            name=name,
            request_id=request_id,
            decision=decided,
        )

    consume(con, request_id)
    publish(
        con,
        "request.consumed",
        actor,
        {"kind": "confirm_effect", "owner": config.owner, "name": name},
        job_id=job_id,
        request_id=request_id,
        idem_key=f"req:{request_id}:consumed",
    )

    fn = f"{config.owner}/{name}"
    row = outbox.enqueue(
        con,
        op=outbox.REPO_CREATE_OP,
        args={
            "owner": config.owner,
            "name": name,
            "full_name": fn,
            "private": True,
            "auto_init": False,
            "confirmed_by": request_id,
        },
        idem_key=f"{outbox.REPO_CREATE_OP}:{fn.casefold()}",
        at_most_once=True,
        max_attempts=1,
    )
    made = run_repo_create(
        con,
        row_id=row.id,
        transport=transport,
        config=config,
        actor=actor,
        job_id=job_id,
        claimed_by=claimed_by or actor,
    )
    if made.state != "collision":
        return made

    # Free when we asked, taken by the time we called. Same loop as a collision
    # found up front: a spoken rename, never a suffix.
    reason = (
        f"Somebody created {fn} between my asking you and my creating it, so I stopped. "
        "I am not going to add a number to the end of it."
    )
    req = confirm.raise_rename(
        con, owner=config.owner, rejected=name, reason=reason, actor=actor, job_id=job_id
    )
    return Creation(
        "collision",
        config.owner,
        f"{reason} {confirm.RENAME_QUESTION}",
        name=name,
        outbox_id=row.id,
        request_id=req.id,
    )


def run_repo_create(
    con: sqlite3.Connection,
    *,
    row_id: str,
    transport: Transport,
    config: ProjectConfig,
    actor: str,
    job_id: str | None = None,
    claimed_by: str = "desk",
) -> Creation:
    """Execute ONE at-most-once outbox row. Never retries, never guesses.

    Split from :func:`create` because the row outlives the process that wrote it:
    a worker started tomorrow runs exactly this function against the same row.

    The order at the end is deliberate. The effect rows are written BEFORE the
    outbox row is marked done, because a crash in between leaves a row a sweeper
    escalates to a human who can see the repository — whereas the other order
    would leave a repository the LEDGER has never heard of, which is how the same
    repository gets created twice.
    """
    row = outbox.get(con, row_id)
    if row is None:
        raise KeyError(row_id)
    owner = str(row.args.get("owner") or config.owner)
    name = str(row.args.get("name") or "")
    fn = str(row.args.get("full_name") or f"{owner}/{name}")

    if row.state == "done":
        result = row.result or {}
        return Creation(
            "created",
            owner,
            f"{fn} is already there — I created it earlier.",
            name=name,
            outbox_id=row.id,
            create_effect_id=row.effect_id,
            live_effect_id=result.get("live_effect_id"),
        )
    if row.state == "needs_human":
        return Creation(
            "needs_human", owner, outbox.spoken_escalation(row), name=name, outbox_id=row.id
        )
    if row.state == "failed":
        return Creation(
            "refused",
            owner,
            f"I already tried to create {fn} and GitHub refused: {row.error}. Nothing was created.",
            name=name,
            outbox_id=row.id,
        )
    if not outbox.begin(con, row.id, claimed_by):
        return Creation(
            "busy",
            owner,
            f"Something else is already creating {fn}, so I have left it alone.",
            name=name,
            outbox_id=row.id,
        )

    try:
        repo = gh.create(transport, owner, name, private=True, auto_init=False)
    except TransportError as exc:
        # THE case this whole mechanism exists for. Whether the repository now
        # exists is unknown, so nothing here decides: the row goes to a human and
        # is never attempted again.
        outbox.escalate(con, row.id, f"{type(exc).__name__}: {exc}")
        return Creation(
            "needs_human",
            owner,
            outbox.spoken_escalation(outbox.get(con, row.id) or row),
            name=name,
            outbox_id=row.id,
        )
    except gh.NotAsRequested as exc:
        # It exists and it is PUBLIC, which is the one safety property this whole
        # design rests on. The spine refuses to record a creation that is not
        # private and empty, so the ledger's record of it is the compensatable row
        # below plus an outbox row a human has to look at.
        live = _record_live(
            con,
            repo=exc.repo,
            caps=config.capabilities,
            job_id=job_id,
            confirmed_by=row.args.get("confirmed_by"),
            actor=actor,
        )
        outbox.escalate(con, row.id, f"created but not as requested: {exc.reason}")
        spoken = (
            f"{exc.repo.full_name} was created PUBLIC although I asked for private, so the one "
            "thing that made a wrong repository harmless did not hold. I have not started "
            "anything in it. Say undo and I will do what I can."
        )
        return Creation(
            "needs_human",
            owner,
            spoken,
            name=name,
            outbox_id=row.id,
            live_effect_id=live.id if live is not None else None,
            repo=exc.repo,
        )
    except GithubError as exc:
        if gh.name_already_exists(exc):
            outbox.refused(con, row.id, f"{fn} already exists")
            return Creation(
                "collision", owner, f"{fn} already exists.", name=name, outbox_id=row.id
            )
        outbox.refused(con, row.id, str(exc))
        return Creation(
            "refused",
            owner,
            f"GitHub refused to create {fn} — {exc.status} {exc.message} — so nothing was created.",
            name=name,
            outbox_id=row.id,
        )

    created = fx.github_repo_create_effect(
        con,
        full_name=repo.full_name,
        private=repo.private,
        empty=repo.empty is not False,
        job_id=job_id,
        confirmed_by=row.args.get("confirmed_by"),
        actor=actor,
    )
    live = _record_live(
        con,
        repo=repo,
        caps=config.capabilities,
        job_id=job_id,
        confirmed_by=row.args.get("confirmed_by"),
        actor=actor,
    )
    outbox.done(
        con,
        row.id,
        result={
            **repo.as_provider_ref(),
            "clone_url": clone_url(repo.full_name),
            "live_effect_id": live.id if live is not None else None,
        },
        effect_id=created.id,
    )
    return Creation(
        "created",
        owner,
        fx.spoken_effect_line(live if live is not None else created),
        name=repo.name,
        outbox_id=row.id,
        create_effect_id=created.id,
        live_effect_id=live.id if live is not None else None,
        repo=repo,
    )


def _record_live(
    con: sqlite3.Connection,
    *,
    repo: gh.Repo,
    caps: Capabilities,
    job_id: str | None,
    confirmed_by: str | None,
    actor: str,
) -> fx.Effect | None:
    """The compensatable row, or None when this token can do nothing to it.

    A second plan-less irreversible row would add a line to the ledger saying
    exactly what the first one says. Not writing it is what makes "undo that" fall
    through to ``github.repo_create`` and speak the honest refusal with its reason.
    """
    plan = compensate.plan_for(caps, owner=repo.owner, slug=repo.name)
    if plan is None:
        return None
    return fx.record_effect(
        con,
        kind=REPO_LIVE_KIND,
        summary=f"I made {repo.full_name} the live repository for this project",
        reversibility=compensate.ledger_class(caps),
        job_id=job_id,
        provider_ref={**repo.as_provider_ref(), "clone_url": clone_url(repo.full_name)},
        undo_plan=plan,
        confirmed_by=confirmed_by,
        actor=actor,
    )


# ───────────────────────────── undo ─────────────────────────────


def undoable_repo_effect(
    con: sqlite3.Connection,
    *,
    job_id: str | None = None,
    repo_full_name: str | None = None,
    limit: int = 50,
) -> fx.Effect | None:
    """The row "undo that" means for a repository, newest first.

    Prefers the compensatable ``github.repo_live`` row, because that is the one
    something can be done about, and falls back to ``github.repo_create`` so the
    user hears the honest refusal rather than "I can't find that".
    """
    rows = fx.recent_effects(con, limit=limit, job_id=job_id)
    matching = [
        e
        for e in rows
        if repo_full_name is None
        or (e.provider_ref or {}).get("full_name") == repo_full_name
        or (e.provider_ref or {}).get("was") == repo_full_name
    ]
    for kind in (REPO_LIVE_KIND, REPO_CREATE_KIND):
        for e in matching:
            if e.kind == kind:
                return e
    return None


def undo_repo(
    con: sqlite3.Connection,
    *,
    actor: str,
    effect_id: str | None = None,
    job_id: str | None = None,
    repo_full_name: str | None = None,
) -> fx.UndoResult:
    """Run whatever compensation the token supports, and say what it could not do.

    With ``effect_id`` it undoes exactly that row: a caller that names a row is
    never quietly redirected to a different one. Without it, the row is resolved
    by :func:`undoable_repo_effect`, which is what "undo that" means.

    The handler must already be registered in THIS process
    (:func:`jarvis.project.compensate.register_repo_compensator`). If it is not,
    the spine reports ``no_handler`` and says so, rather than pretending the
    compensation is impossible.

    THE SENTENCE IS SWAPPED FOR THE COMPENSATION'S OWN, and that is the point of
    this wrapper. :func:`jarvis.effects.undo` builds ``spoken`` from the row it
    was asked to undo, which for a success reads "that one is already dealt with"
    — true of the row, and a lie about the world whenever the compensation was
    PARTIAL. A channel speaking the field called ``spoken`` would tell the user
    the repository had been dealt with while it sits there under its original
    name because the rename was refused. The honest sentence is already written,
    on the compensation row; this puts it where the caller will actually say it.
    Generic in the spine, specific here: only this layer knows that its
    compensation can half-succeed.
    """
    target = effect_id
    if target is None:
        found = undoable_repo_effect(con, job_id=job_id, repo_full_name=repo_full_name)
        if found is None:
            raise fx.UnknownEffect("no repository effect to undo")
        target = found.id
    result = fx.undo(con, target, actor=actor)
    if result.undo_effect_id is None:
        return result
    compensation = fx.get_effect(con, result.undo_effect_id)
    if compensation is None or not compensation.summary:
        return result
    return replace(result, spoken=compensation.summary)


# ───────────────────────────── workspace and job ─────────────────────────────


def prepare_workspace(
    *,
    config: ProjectConfig,
    name: str,
    clone_from: str | None = None,
    git: Git,
    token: str | None = None,
) -> Workspace:
    """Clone into ``<workspace_root>/<name>``, or refuse with a typed reason.

    ``token`` comes from the CALLER on every call, and ``clone_from`` defaults to
    the HTTPS URL for ``owner/name`` — :class:`jarvis.github.repos.Repo` carries
    ``html_url`` rather than a clone URL, and deriving it is one string instead of
    another request.
    """
    return prepare_clone(
        root=config.workspace_root,
        name=name,
        clone_url=clone_from or clone_url(f"{config.owner}/{name}"),
        git=git,
        token=token,
    )


def start_build_job(
    con: sqlite3.Connection,
    *,
    config: ProjectConfig,
    name: str,
    spoken_name: str,
    cwd: Path,
    created_by: str,
    prompt_text: str | None = None,
    prompt_request_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> jobs.Job:
    """The ``claude_code`` job row pointing at the clone. Stage 2 runs in it unchanged.

    ``title`` is the words the user said rather than the slug: the title is SPOKEN
    in the briefing and "comment watcher" is what they will recognise, while
    ``repo`` and ``cwd`` carry the exact machine-readable forms.
    """
    return jobs.create_job(
        con,
        kind="claude_code",
        title=" ".join(spoken_name.split()) or name,
        created_by=created_by,
        actor=created_by,
        cwd=str(cwd),
        repo=f"{config.owner}/{name}",
        host=config.host,
        model=model,
        effort=effort,
        prompt_text=prompt_text,
        prompt_request_id=prompt_request_id,
    )
