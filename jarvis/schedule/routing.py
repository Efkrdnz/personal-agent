"""Which channels get asked, and how long each one waits. Never who dials.

This is the only file in the component that knows a channel exists, and it knows
them as STRINGS. ``presence`` says which channels can reach a human right now;
:func:`ladder` turns that into delivery rows; a channel process picks its own
rows up. Nothing here imports Telegram, the desk, or — when it exists — the
phone, which is the whole reason stage 6 can be a re-wiring.

THE SCHEDULER NEVER DIALS. A phone rung is only ever written for a request the
caller marked ``high`` or ``critical``, and neither a briefing nor a
task-finished notice is ever either of those. The morning briefing escalating to
a call is a stage-6 decision about the ladder's data, not a change here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from jarvis import requests as rq
from jarvis.ids import now
from jarvis.jobs import shift_ts
from jarvis.presence import evaluate_presence

__all__ = [
    "CHANNEL_ORDER",
    "ESCALATE_PHONE_S",
    "ESCALATE_TELEGRAM_S",
    "URGENCIES_THAT_MAY_RING",
    "deliver",
    "ladder",
]

#: Preference order, not reachability order: the desk is free and immediate, the
#: phone costs money and interrupts. Reachability is presence's to decide.
CHANNEL_ORDER: tuple[str, ...] = ("desk", "telegram", "phone")

#: The spine's own default escalation gap (``requests.escalate_after_s``). A
#: question spoken into an empty room gets thirty seconds of silence plus a
#: minute of grace before Telegram also asks it.
ESCALATE_TELEGRAM_S = 90

#: Fifteen minutes, from the stage-6 entry in the roadmap: "a blocking request
#: unanswered for fifteen minutes makes your phone ring".
ESCALATE_PHONE_S = 900

#: Only these may write a phone rung. A briefing is ``low`` and a task-finished
#: notice is ``low``; that is what makes "the scheduler never dials" structural
#: rather than a promise in a docstring.
URGENCIES_THAT_MAY_RING: frozenset[str] = frozenset(("high", "critical"))

_DELAY_S: dict[str, int] = {
    "desk": 0,
    "telegram": ESCALATE_TELEGRAM_S,
    "phone": ESCALATE_PHONE_S,
}


def ladder(reachable: Sequence[str], *, urgency: str = "normal") -> tuple[tuple[str, int], ...]:
    """``(channel_kind, delay_seconds)`` rungs, in preference order. PURE.

    The FIRST rung is always immediate, whichever channel it turns out to be:
    when the user is out, Telegram is not an escalation from the desk, it is the
    only way to reach them, and making it wait ninety seconds would be ninety
    seconds of nothing for no reason.
    """
    rungs = [
        (kind, _DELAY_S[kind])
        for kind in CHANNEL_ORDER
        if kind in reachable and (kind != "phone" or urgency in URGENCIES_THAT_MAY_RING)
    ]
    if rungs:
        rungs[0] = (rungs[0][0], 0)
    return tuple(rungs)


def deliver(
    con: sqlite3.Connection,
    req: rq.Request,
    *,
    now_ts: str | None = None,
    deliver_after: str | None = None,
    attempt: int = 1,
) -> list[rq.Delivery]:
    """Materialise this request's ladder, asking presence where the user is.

    ``deliver_after`` is the snooze: the first rung is due then instead of now,
    and every later rung is staggered from it. It is a column on the delivery row
    and nothing else — see :mod:`jarvis.schedule.gate` — so postponing a question
    costs one row and touches no schedule.

    Routes by ASKING presence rather than by guessing, and re-asks on every
    attempt: the user who was at the desk when the briefing was offered may be in
    the car five minutes later.
    """
    ts = now_ts or now()
    base = deliver_after or ts
    verdict = evaluate_presence(con, ts)
    out: list[rq.Delivery] = []
    for kind, delay in ladder(verdict.reachable, urgency=req.urgency):
        out.append(rq.schedule_delivery(con, req.id, kind, shift_ts(base, delay), attempt=attempt))
    return out
