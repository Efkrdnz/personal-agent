"""THE HOP: a sentence somebody said out loud becomes a row another process runs.

This is the smallest module in the tree with the largest claim on it, so the
claim is stated plainly. "Let's build an app that watches my YouTube comments"
arrives here as a Gemini function call and leaves as ONE ``repo_setup`` job row.
Nothing is created, cloned, or written to GitHub here, and nothing is spoken:
the builder process reads the row, tidies the transcript, reads the list back,
waits for a yes, and only then makes a repository. That process may be started
tomorrow, on a machine that has rebooted twice since anybody spoke.

THE ARGUMENT IS NOT THE CONTRACT. The model calls this tool with its own summary
of what it heard, and that summary is a paraphrase — the exact thing R2 says the
tidied list must never be. So the paraphrase is used for the TITLE, which is
decoration, and the fidelity chain in :mod:`jarvis.spec` is run against
``ctx.extra["transcript"]``: the user's own words, from the Live session's input
transcription or from the text they typed. If a channel cannot produce those
words, this tool REFUSES rather than quietly promoting the paraphrase to the
contract — a containment check run against a paraphrase passes every time and
proves nothing, which is worse than no check because it reads like one.

MODEL AND EFFORT ARE PARSED HERE, NOT ASKED FOR. :func:`jarvis.spec.parse_model`
and :func:`jarvis.spec.parse_effort` are pure functions over the user's own
words, so "use Opus with max effort" is honoured deterministically and a model
that hallucinated ``model="opus-9"`` into the call cannot change what runs. They
are deliberately NOT tool parameters for that reason.
"""

from __future__ import annotations

import sqlite3

from jarvis import jobs, spec
from jarvis.bus import publish
from jarvis.ids import dedupe_key
from jarvis.project.mode import resolve as resolve_mode
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Tool, ToolError

__all__ = [
    "BUILD_JOB_KIND",
    "TRANSCRIPT",
    "NoTranscript",
    "AlreadyBuilding",
    "code_build",
    "tool",
]

#: The job kind the builder process polls for. The spine's frozen schema already
#: names it in the ``jobs.kind`` comment: claude_code|outbound_call|briefing|
#: repo_setup|undo. A ``repo_setup`` job becomes a ``claude_code`` job only after
#: a human has said yes to a name and a list.
BUILD_JOB_KIND = "repo_setup"

#: The key in :attr:`ToolCtx.extra` holding the user's OWN words. See the module
#: docstring; this is the one input here that is load-bearing.
TRANSCRIPT = "transcript"

#: States in which a ``repo_setup`` job is still somebody's problem.
_LIVE_STATES = ("queued", "starting", "running", "blocked", "deferred", "parked", "finishing")


class NoTranscript(ToolError):
    """The channel could not say what the user actually said."""

    def __init__(self) -> None:
        super().__init__(
            "I didn't catch your own words clearly enough to build from them, and I'm not "
            "going to build from my summary of them. Say it again?"
        )


class AlreadyBuilding(ToolError):
    """One at a time. The second call is almost always the model retrying."""

    def __init__(self, title: str) -> None:
        super().__init__(
            f"I'm already setting up {title}. Let me finish that one first — "
            "say 'cancel that' if you'd rather start over."
        )
        self.title = title


def _transcript_of(ctx: ToolCtx) -> str:
    text = str(ctx.extra.get(TRANSCRIPT) or "").strip()
    if not text:
        raise NoTranscript()
    return text


def _title_for(project_name: str, summary: str, transcript: str) -> str:
    """What the briefing will SAY about this build, in descending order of trust.

    The user's own spoken name first, then the model's summary, then the opening
    of the transcript. All three are decoration — the contract is the transcript —
    so the fallbacks are allowed to be rough.
    """
    for candidate in (project_name, summary):
        cleaned = " ".join(str(candidate or "").split())
        if cleaned:
            return cleaned[:120]
    opening = " ".join(transcript.split())[:60]
    return f"the build you described ({opening}…)" if opening else "an unnamed build"


def _in_flight(con: sqlite3.Connection) -> jobs.Job | None:
    marks = ", ".join("?" for _ in _LIVE_STATES)
    row = con.execute(
        f"SELECT * FROM jobs WHERE kind=? AND state IN ({marks}) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (BUILD_JOB_KIND, *_LIVE_STATES),
    ).fetchone()
    return None if row is None else jobs.to_job(row)


def code_build(ctx: ToolCtx, project_name: str = "", summary: str = "") -> str:
    """File a build request and say what happens next. Returns in milliseconds.

    The acknowledgement names the three things the user can act on immediately —
    what it will be called, which model, and the cloud caveat if they asked for
    one — and promises the read-back rather than performing it, because the
    read-back needs a tidier, a reader voice and an answer, none of which belong
    inside a function the conversation is waiting on.
    """
    transcript = _transcript_of(ctx)

    if existing := _in_flight(ctx.con):
        raise AlreadyBuilding(existing.title)

    model_ask = spec.parse_model(transcript)
    effort_ask = spec.parse_effort(transcript)
    title = _title_for(project_name, summary, transcript)

    job = jobs.create_job(
        ctx.con,
        kind=BUILD_JOB_KIND,
        title=title,
        created_by=ctx.actor,
        actor=ctx.actor,
        # The user's own words, stored once and never rewritten. The tidied list
        # is derived from this column every time, so an edit to the list can
        # always be checked back against what was actually said.
        prompt_text=transcript,
        model=model_ask.alias,
        effort=effort_ask.level,
    )

    # Recorded against the job so six months of "did anyone ask for the cloud?"
    # is one SELECT rather than a memory. ADR 0009.
    mode = resolve_mode(ctx.con, utterance=transcript, actor=ctx.actor, job_id=job.id)

    publish(
        ctx.con,
        "project.requested",
        ctx.actor,
        {
            "title": title,
            "channel": ctx.channel,
            "model": model_ask.alias,
            "model_phrase": model_ask.phrase,
            "effort": effort_ask.level,
            "effort_phrase": effort_ask.phrase,
            "spoken_name": " ".join(str(project_name or "").split()) or None,
        },
        job_id=job.id,
        idem_key=f"project:{dedupe_key(job.id, 'requested', transcript)}",
    )

    return " ".join(part for part in _acknowledgement(title, model_ask, effort_ask, mode) if part)


def _acknowledgement(
    title: str,
    model_ask: spec.ModelAsk,
    effort_ask: spec.EffortAsk,
    mode: object,
) -> tuple[str | None, ...]:
    """The sentence said while the builder is still starting.

    Every clause here is either what was HEARD or what is about to happen. There
    is deliberately no "I've created the repository" in it: nothing has been
    created, and an acknowledgement that describes the finished state is how a
    demo becomes a lie the first time the builder falls over.
    """
    head = f"Right — {title}."
    who = model_ask.phrase or model_ask.alias
    which = f"I'll run that on {who}." if who else None
    # 'high effort' is Claude Code's own default, so hearing it echoed back as a
    # change is the one place this tool could mislead without saying anything false.
    effort = None
    if effort_ask.level and effort_ask.level == spec.EFFORT_THAT_IS_ALREADY_THE_DEFAULT:
        effort = "High effort is already the default, so that changes nothing."
    elif effort_ask.level:
        effort = f"{effort_ask.level.capitalize()} effort."
    elif effort_ask.phrase:
        effort = (
            f"I heard '{effort_ask.phrase}' but I don't know that effort level, "
            "so I'm leaving it at the default."
        )
    caveat = getattr(mode, "spoken", None) if getattr(mode, "honest", False) else None
    tail = "Give me a moment and I'll read the requirements back to you before anything is created."
    return (head, which, effort, caveat, tail)


tool = Tool(
    name="code_build",
    description=(
        "Start a new coding project from what the user just said out loud. Use this when "
        "they describe something they want built — 'let's build an app that…', 'make me a "
        "script that…', 'I want a bot that…'. Do NOT use it to answer questions about an "
        "existing project; that is project_status. It files the request and returns "
        "immediately: the requirements are read back for confirmation afterwards, and "
        "nothing is created on GitHub until the user says yes."
    ),
    handler=code_build,
    parameters={
        "type": "OBJECT",
        "properties": {
            "project_name": {
                "type": "STRING",
                "description": (
                    "The name the user said for the project, in THEIR words "
                    "('comment watcher'). Empty if they did not name it."
                ),
            },
            "summary": {
                "type": "STRING",
                "description": (
                    "One short line describing the build, for the activity log. This is "
                    "never treated as the requirements — those are taken from the user's "
                    "own words — so do not try to be complete here."
                ),
            },
        },
    },
    # The desk and Telegram can start a build. The phone deliberately cannot:
    # a build begins with a repository being created under the user's account,
    # and the phone leg is the one anybody who knows the number can reach.
    channels=("desk", "telegram", "cli"),
    long_running=True,
)
