"""A job finished. The news goes wherever presence says the user is.

THE SAME MACHINERY, WITH A DIFFERENT KIND. A task-finished notice is raised as a
request, routed through the same ladder as the morning gate, and resolved by the
same typed timeout. Nothing here decides between speaking and messaging: it asks
:mod:`jarvis.presence` and writes delivery rows, exactly as the gate does.

WHERE THE CLAIM STRAINED, and it is worth writing down. A notice is a request
whose answer is OPTIONAL, and the spine's ``ReqKind`` has no word for that: the
kinds are a closed set in a file this stage may not edit, so this borrows
``free_text``. It reads correctly — "the build failed; anything you want done?"
really is a free-text question with two shortcuts — but the row does not say
"this was news", and a reader of the requests table cannot tell a notice from a
question by its kind alone.

THE WORDING IS NOT OURS. The sentence comes from
:func:`jarvis.reconcile.project_status`, which is what the morning briefing reads
out. Two sentences for one event would drift, and the one a person hears at 10:00
would stop matching the one they heard at 15:00 the day before.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jarvis import jobs
from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.ids import dedupe_key
from jarvis.reconcile import project_status

__all__ = [
    "COMPLETION_EXPIRES_S",
    "COMPLETION_QUESTION",
    "LEAVE_IT_LABEL",
    "TELL_ME_LABEL",
    "TERMINAL_STATES",
    "Notice",
    "completion_line",
    "leave_it_answer",
    "raise_completion",
]

COMPLETION_QUESTION = "Anything you want me to do about it?"
TELL_ME_LABEL = "Tell me more"
LEAVE_IT_LABEL = "Nothing right now"

#: Two hours, then it resolves itself to "nothing right now". A notice that
#: nobody answered is not a decision left hanging; it is news that got old.
COMPLETION_EXPIRES_S = 7200

#: The job states that are news. ``killed`` is included because the user almost
#: always did it and hearing nothing back is unsettling; the WORDING for it comes
#: from reconcile, which words it as "stopped" rather than "failed".
TERMINAL_STATES: frozenset[str] = frozenset(("done", "failed", "killed"))


@dataclass(frozen=True, slots=True)
class Notice:
    """One finished job, the sentence about it, and the row that carries it."""

    job_id: str
    state: str
    line: str
    request: rq.Request


def leave_it_answer() -> rq.Answer:
    return {
        "answers": {COMPLETION_QUESTION: LEAVE_IT_LABEL},
        "text": LEAVE_IT_LABEL,
        "sources": {COMPLETION_QUESTION: "option"},
    }


def completion_line(con: sqlite3.Connection, job: jobs.Job, *, now_ts: str | None = None) -> str:
    """The sentence the morning briefing would use for this job, said now instead.

    ``project_status`` is asked for the window starting at this job's own
    ``updated_at`` and the note for this job id is picked out of it. Reading it
    does NOT advance the briefing cursor (that is
    :func:`jarvis.reconcile.set_briefing_cursor`, called only after a briefing was
    actually said), so a job mentioned here is still mentioned tomorrow morning —
    which is the cursor's own "repeating beats dropping" choice, honoured rather
    than worked around.
    """
    status = project_status(con, since=job.updated_at, now_ts=now_ts)
    for note in (*status.finished, *status.failed):
        if note.job_id == job.id:
            return note.line
    return f"{job.title} is no longer running."


def raise_completion(
    con: sqlite3.Connection,
    job_id: str,
    *,
    actor: str = "scheduler",
    expires_in_s: int | None = COMPLETION_EXPIRES_S,
    now_ts: str | None = None,
) -> Notice | None:
    """Raise the notice for a finished job, or None if it has not finished.

    Idempotent per (job, state): the dedupe key is hashed over both, so a tick
    that sees the same finished job twice — and it will, because the cursor that
    finds them is inclusive — returns the row it already made.
    """
    job = jobs.get(con, job_id)
    if job is None or job.state not in TERMINAL_STATES:
        return None
    line = completion_line(con, job, now_ts=now_ts)
    pres = rq.make_presentation(
        intro=f"{line} {COMPLETION_QUESTION}",
        options=(
            {"label": TELL_ME_LABEL, "description": "read me the detail"},
            {"label": LEAVE_IT_LABEL, "description": "nothing, thanks"},
        ),
        verbatim=True,
        multi=False,
        allows_free_text=True,
        free_text_prompt="Or say what you want done about it.",
        question=COMPLETION_QUESTION,
        dtmf_map={"1": 1, "2": 2},
        default_answer=leave_it_answer(),
    )
    req = rq.create_request(
        con,
        kind="free_text",
        short_label=job.title,
        presentation=pres,
        payload={"job_id": job.id, "state": job.state, "line": line},
        actor=actor,
        job_id=job.id,
        # Low, like the gate: news never rings a telephone.
        urgency="low",
        expires_in_s=expires_in_s,
        on_timeout="default",
        dedupe_key=dedupe_key(job.id, "schedule.completion", {"state": job.state}),
    )
    publish(
        con,
        "request.created",
        actor,
        {
            "kind": "free_text",
            "short_label": req.short_label,
            "job_state": job.state,
            "line": line,
        },
        job_id=job.id,
        request_id=req.id,
        idem_key=f"req:{req.id}:created",
    )
    return Notice(job_id=job.id, state=job.state, line=line, request=req)
