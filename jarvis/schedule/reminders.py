"""A reminder comes due. It becomes a request, and goes wherever the user is.

The same machinery as the morning gate and the task-finished notice: a request
row, routed by presence, settled by an answer or a typed timeout. A reminder is
raised only WHEN IT IS DUE — not when it was set — so presence is asked at six
o'clock about where the user is at six o'clock.

It borrows ``free_text`` for the same reason the completion notice does (see
:mod:`jarvis.schedule.completion`): a reminder is news whose answer is optional,
and the spine's kinds have no word for that.

SNOOZE IS A NEW REMINDER, not a moved one. "In ten minutes" answers this request
and adds a reminder ten minutes out, which is the honest shape CLAUDE.md
records for the gate's snooze: an answered request cannot also stay pending.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from jarvis import memory
from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.db import tx
from jarvis.ids import dedupe_key, now, parse_ts
from jarvis.schedule.routing import deliver

__all__ = [
    "GOT_IT",
    "REMINDER_QUESTION",
    "SNOOZE",
    "SNOOZE_S",
    "Fired",
    "fire_reminders",
    "raise_reminder",
    "settle_reminders",
]

REMINDER_QUESTION = "Reminder"
GOT_IT = "Got it"
SNOOZE = "In ten minutes"
SNOOZE_S = 600

#: An hour, then it settles itself as "got it". A reminder nobody heard for an
#: hour is still in the log; it is not still worth interrupting for.
REMINDER_EXPIRES_S = 3600


@dataclass(frozen=True, slots=True)
class Fired:
    reminder: memory.Reminder
    request: rq.Request
    snoozed_to: str | None = None


def _got_it() -> rq.Answer:
    return {
        "answers": {REMINDER_QUESTION: GOT_IT},
        "text": GOT_IT,
        "sources": {REMINDER_QUESTION: "option"},
    }


def raise_reminder(
    con: sqlite3.Connection, r: memory.Reminder, *, actor: str = "scheduler"
) -> rq.Request:
    """The request for one reminder. Idempotent per reminder, so a retry is free."""
    pres = rq.make_presentation(
        intro=f"Reminder: {r.text}",
        options=(
            {"label": GOT_IT, "description": "done"},
            {"label": SNOOZE, "description": "remind me again in ten minutes"},
        ),
        verbatim=True,
        multi=False,
        allows_free_text=False,
        question=REMINDER_QUESTION,
        dtmf_map={"1": 1, "2": 2},
        default_answer=_got_it(),
    )
    return rq.create_request(
        con,
        kind="free_text",
        short_label=f"reminder: {r.text}"[:80],
        presentation=pres,
        payload={"reminder_id": r.id, "text": r.text, "due_at": r.due_at},
        actor=actor,
        # Normal, not low: the user ASKED to be interrupted for this, which is
        # the difference between a reminder and news.
        urgency="normal",
        expires_in_s=REMINDER_EXPIRES_S,
        on_timeout="default",
        dedupe_key=dedupe_key(r.id, "reminder"),
    )


def fire_reminders(
    con: sqlite3.Connection, *, actor: str = "scheduler", now_ts: str | None = None
) -> list[Fired]:
    """Raise and route every reminder that is due."""
    ts = now_ts or now()
    out: list[Fired] = []
    for r in memory.due_reminders(con, now_ts=ts):
        req = raise_reminder(con, r, actor=actor)
        if not memory.mark_fired(con, r.id, req.id):
            continue  # another scheduler got there; it routes it
        deliver(con, req, now_ts=ts)
        publish(
            con,
            "reminder.fired",
            actor,
            {"reminder_id": r.id, "due_at": r.due_at, "late_s": _late(r.due_at, ts)},
            request_id=req.id,
            idem_key=f"reminder:{r.id}:fired",
        )
        out.append(Fired(r, req))
    return out


def settle_reminders(
    con: sqlite3.Connection, *, actor: str = "scheduler", now_ts: str | None = None
) -> list[Fired]:
    """Act on every answered reminder: done, or a new one ten minutes out."""
    ts = now_ts or now()
    out: list[Fired] = []
    for r in memory.fired_reminders(con):
        req = rq.get_request(con, r.request_id or "")
        if req is None:
            continue
        if req.state in ("expired", "cancelled", "superseded"):
            memory.settle(con, r.id)
            continue
        if req.state not in ("answered", "consumed"):
            continue
        snooze = _label(req) == SNOOZE
        # One transaction: settling and snoozing are one decision, and a crash
        # between them must not lose the snooze or make two.
        with tx(con):
            if not memory.settle(con, r.id):
                continue
            later = None
            if snooze:
                due = parse_ts(ts) + timedelta(seconds=SNOOZE_S)
                later = memory.add_reminder(con, r.text, due, actor=actor, channel=r.channel)
        out.append(Fired(r, req, snoozed_to=later.due_at if later else None))
    return out


def _label(req: rq.Request) -> str:
    answer = req.answer or {}
    picked = (answer.get("answers") or {}).get(REMINDER_QUESTION)
    return picked if isinstance(picked, str) else str(answer.get("text") or "")


def _late(due_at: str, ts: str) -> float:
    return max(0.0, (parse_ts(ts) - parse_ts(due_at)).total_seconds())
