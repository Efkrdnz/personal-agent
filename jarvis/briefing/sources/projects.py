"""Section one: project status. :func:`jarvis.reconcile.project_status` ALREADY WRITES THIS.

This module does not compose a single sentence of its own about a job. It calls
the function the spine already has, whose ``lines`` field exists precisely to be
spoken, and does exactly two things to the result:

*It splits EVENTS from STANDING FACTS.* "The scraper build failed" happened once
and must not be said twice. "The todo app has been waiting two hours for an
answer" is true right now and must be said every morning until it stops being
true. The briefing's ``seen`` set therefore applies to the first kind and must
never touch the second — dedupe a standing fact and a question waiting since
Tuesday goes unmentioned on Wednesday, which is the exact failure the briefing
exists to prevent.

*It subtracts, rather than recomposing.* The event lines are removed from
``status.lines`` BY EXACT STRING, because every :class:`~jarvis.reconcile.JobNote`
carries the sentence it contributed. Rebuilding the spoken lines here would be a
second composer for section one, and the two would drift the first time somebody
improved the wording in one of them.

The cursor is :data:`jarvis.reconcile.BRIEFING_CURSOR` and it is written by
:func:`jarvis.reconcile.set_briefing_cursor`, not by this package's generic
cursor writer. That function is where the INCLUSIVE decision is documented and
it is the only place that decision should live.
"""

from __future__ import annotations

import sqlite3

from jarvis.briefing.sources import CandidateItem, Fetch, down
from jarvis.reconcile import BLOCKED_BRIEFING_S, BRIEFING_CURSOR, JobNote, project_status

__all__ = ["PROJECTS_CURSOR", "fetch", "note_id"]

#: Named through :mod:`jarvis.reconcile` rather than re-spelled, so the briefing
#: and the reconciler can never end up reading two different rows.
PROJECTS_CURSOR = BRIEFING_CURSOR


def note_id(note: JobNote) -> str:
    """The identity a finished-or-failed job has in the ``briefing_seen`` set.

    The STATE is part of the id on purpose: a job that was orphaned, picked back
    up and then failed is two different things to say, and keying on the job id
    alone would swallow the second one.
    """
    return f"job:{note.job_id}:{note.state}"


def fetch(
    con: sqlite3.Connection,
    cursor: str | None,
    *,
    now_ts: str | None = None,
    blocked_threshold_s: float = BLOCKED_BRIEFING_S,
) -> Fetch:
    """Section one's candidates and its standing lines.

    Takes the connection FIRST and holds none, like everything else in this tree:
    the process that composes a briefing is routinely not the process that says
    it. That is why this is a function and not a
    :class:`~jarvis.briefing.sources.Source` — the other three sections read a
    remote service over HTTPS, and this one reads the same SQLite file the
    briefing itself lives in.
    """
    try:
        status = project_status(
            con,
            since=cursor,
            blocked_threshold_s=blocked_threshold_s,
            now_ts=now_ts,
        )
    except sqlite3.Error as exc:
        # The local database is the one source whose failure is not routine. It
        # still must not take the rest of the briefing down with it.
        return down(f"I could not read your project status: {exc}")

    events = (*status.finished, *status.failed)
    event_lines = {n.line for n in events}
    return Fetch(
        items=tuple(
            CandidateItem(id=note_id(n), line=n.line, detail=n.state, at=None) for n in events
        ),
        standing=tuple(line for line in status.lines if line not in event_lines),
        # What set_briefing_cursor would write. Handed back rather than written:
        # the cursor moves only after this section has actually been delivered.
        next_cursor=now_ts,
        notes={"since": status.since, "open_requests": str(status.open_requests)},
    )
