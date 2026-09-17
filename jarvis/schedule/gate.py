"""The morning gate: "Good moment for your briefing?" — Now / Five minutes / Skip today.

ONE ``create_request`` CALL. That is the entire coupling between the thing that
wakes up at ten and everything else in this system. The scheduler does not know
what a briefing contains, cannot compose one, and never speaks: it writes a row
with three options and lets whichever channel can reach a human present it. If
this file ever grows a second way to ask the user something, the claim that a
briefing section is just a request has failed and that is worth saying out loud
before it is worth working around.

"FIVE MINUTES" COSTS ONE ROW. A snooze re-asks the same occurrence at
``attempt+1`` and gives the delivery a later ``due_at``; the schedules table is
not touched, no job is created and nothing is re-armed. The one thing it cannot
do is keep the original row pending: answering is a compare-and-swap that settles
the deliveries in the same transaction, so "postpone" is honestly "you answered
'five minutes', and here is the same question again, due at 10:05".

AND IT IS BOUNDED. Five minutes forever is a loop that asks a sleeping man the
same question until midnight, so after :data:`MAX_SNOOZES` the gate SAYS SO and
skips the day.

THE TIMEOUT PATH AND THE HUMAN PATH CONVERGE. ``on_timeout='default'`` with "Skip
today" as the presentation's own default means an unanswered gate resolves
through the same compare-and-swap a tap would use, and arrives here as an
ordinary answered request. There is no second code path for "nobody was in".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from jarvis import requests as rq
from jarvis.bus import publish
from jarvis.ids import dedupe_key, now
from jarvis.jobs import shift_ts

__all__ = [
    "FIVE_LABEL",
    "GATE_EXPIRES_S",
    "GATE_OPTIONS",
    "GATE_QUESTION",
    "MAX_SNOOZES",
    "NOW_LABEL",
    "SKIP_LABEL",
    "SNOOZE_S",
    "GateChoice",
    "GateOutcome",
    "choice_of",
    "gate_dedupe_key",
    "gate_presentation",
    "handle_answer",
    "raise_gate",
    "skip_answer",
]

GATE_QUESTION = "Good moment for your briefing?"
NOW_LABEL = "Now"
FIVE_LABEL = "Five minutes"
SKIP_LABEL = "Skip today"

#: The frozen options array, in the order they are numbered. An answer is matched
#: against THESE strings; a channel that invents "later" cannot snooze anything.
GATE_OPTIONS: tuple[dict[str, str], ...] = (
    {"label": NOW_LABEL, "description": "read it to me now"},
    {"label": FIVE_LABEL, "description": "ask me again in five minutes"},
    {"label": SKIP_LABEL, "description": "no briefing today"},
)

GateChoice = Literal["now", "five", "skip"]

#: Index -> meaning. The numbering is ours and local (see :mod:`jarvis.answers`),
#: so this mapping is as stable as the array above it.
CHOICE_BY_INDEX: MappingProxyType[int, GateChoice] = MappingProxyType(
    {1: "now", 2: "five", 3: "skip"}
)

#: The roadmap's "call me back in five", literally.
SNOOZE_S = 300

#: Three postponements is fifteen minutes of asking. Past that the answer is
#: obviously not "in five minutes", and continuing to ask is how a helpful system
#: becomes a nuisance that gets muted permanently.
MAX_SNOOZES = 3

#: Two hours. A briefing gate that has been sitting unanswered since ten is not
#: news any more; it resolves itself to "skip today" and the log says the machine
#: decided that, not the user.
GATE_EXPIRES_S = 7200

SNOOZE_LINE = "Right — I'll ask again in five minutes."
EXHAUSTED_LINE = (
    f"That's {MAX_SNOOZES} times now, so I'll leave the briefing until tomorrow "
    "rather than keep asking."
)
SKIP_LINE = "No briefing today, then."
START_LINE = "Right, here it is."
UNREADABLE_LINE = "I couldn't tell which of the three that was, so I've left the briefing."

GateAction = Literal["start", "snoozed", "skipped", "exhausted", "unreadable", "not_answered"]


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """What the answer to one gate meant, and the sentence Jarvis would say."""

    action: GateAction
    request_id: str
    occurrence: str
    spoken: str
    choice: GateChoice | None = None
    next_request: rq.Request | None = None
    deliver_after: str | None = None


def skip_answer() -> rq.Answer:
    """The answer a silent morning resolves to. Written down, never inferred."""
    return {
        "answers": {GATE_QUESTION: SKIP_LABEL},
        "text": SKIP_LABEL,
        "sources": {GATE_QUESTION: "option"},
    }


def gate_presentation() -> rq.Presentation:
    """Three options, no free text, and a keypad map the phone stage will need.

    ``allows_free_text`` is False on purpose: there are exactly three answers to
    this question, and a free-text reply to it would be a sentence about
    something else entirely.
    """
    return rq.make_presentation(
        intro=GATE_QUESTION,
        options=GATE_OPTIONS,
        verbatim=True,
        multi=False,
        allows_free_text=False,
        question=GATE_QUESTION,
        # Written now rather than in stage 6: "a keypress can only ever select an
        # option that was actually offered" has to be true before the channel
        # that presses keys exists.
        dtmf_map={"1": 1, "2": 2, "3": 3},
        default_answer=skip_answer(),
    )


def gate_dedupe_key(schedule: str, occurrence: str) -> str:
    """One key per morning, so two daemons firing at once ask ONE question."""
    return dedupe_key(
        None, "schedule.briefing_gate", {"schedule": schedule, "occurrence": occurrence}
    )


def raise_gate(
    con: sqlite3.Connection,
    *,
    schedule: str,
    occurrence: str,
    actor: str = "scheduler",
    snoozes: int = 0,
    deliver_after: str | None = None,
    expires_in_s: int | None = GATE_EXPIRES_S,
    now_ts: str | None = None,
) -> rq.Request:
    """THE one call. Idempotent per (occurrence, snoozes): re-firing finds the row.

    ``attempt`` is ``snoozes + 1`` rather than :func:`jarvis.requests.next_attempt`
    so the number is a function of what already happened and not of when this
    process looked. Two daemons that both decide to re-ask after the same snooze
    compute the same attempt, hit ``UNIQUE(job_id, dedupe_key, attempt)`` and get
    the same row back.
    """
    ts = now_ts or now()
    req = rq.create_request(
        con,
        kind="briefing_gate",
        short_label="your briefing",
        presentation=gate_presentation(),
        payload={
            "schedule": schedule,
            "occurrence": occurrence,
            "snoozes": snoozes,
            "asked_at": ts,
            "deliver_after": deliver_after,
        },
        actor=actor,
        # Never higher. Urgency is what decides whether a phone may ring, and a
        # briefing that rings a phone is exactly the failure this stage is shaped
        # to avoid.
        urgency="low",
        expires_in_s=expires_in_s,
        on_timeout="default",
        dedupe_key=gate_dedupe_key(schedule, occurrence),
        attempt=snoozes + 1,
    )
    publish(
        con,
        "request.created",
        actor,
        {
            "kind": "briefing_gate",
            "short_label": req.short_label,
            "schedule": schedule,
            "occurrence": occurrence,
            "snoozes": snoozes,
            "deliver_after": deliver_after,
        },
        request_id=req.id,
        idem_key=f"req:{req.id}:created",
    )
    return req


def choice_of(req: rq.Request, answer: rq.Answer) -> GateChoice | None:
    """Which of the three, or None. PURE, and it never guesses.

    The label is looked up in the request's OWN frozen items array, so the only
    strings that can mean "snooze" are the ones this file offered. A free-text
    reply is not one of the three and resolves to None rather than to the nearest
    option — the desk can ask again; a briefing started because somebody said
    something vaguely affirmative is worse than no briefing.
    """
    pres = req.presentation
    question = str(pres.get("question") or "")
    sources = answer.get("sources") or {}
    if sources.get(question) == "free_text":
        return None

    value: Any = (answer.get("answers") or {}).get(question)
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if not isinstance(value, str):
        value = answer.get("text")
    if not isinstance(value, str):
        return None

    for item in pres["items"]:
        if item["label"] == value:
            return CHOICE_BY_INDEX.get(int(item["index"]))
    return None


def handle_answer(
    con: sqlite3.Connection,
    req: rq.Request,
    *,
    actor: str = "scheduler",
    now_ts: str | None = None,
) -> GateOutcome:
    """Act on an answered gate. Safe to run twice on the same row.

    ACT FIRST, CONSUME SECOND, and it is deliberate. Every act here is idempotent
    — an event with a natural ``idem_key``, or a ``create_request`` that returns
    the row it already made — so a daemon that dies between acting and consuming
    repeats itself harmlessly on the next tick. The other order would mark the
    answer used and then lose the morning to a crash.
    """
    ts = now_ts or now()
    payload = req.payload if isinstance(req.payload, dict) else {}
    schedule = str(payload.get("schedule") or "")
    occurrence = str(payload.get("occurrence") or "")
    snoozes = int(payload.get("snoozes") or 0)

    if req.state != "answered" or req.answer is None:
        return GateOutcome("not_answered", req.id, occurrence, "")

    choice = choice_of(req, req.answer)

    if choice == "now":
        publish(
            con,
            "briefing.started",
            actor,
            {
                "schedule": schedule,
                "occurrence": occurrence,
                "answered_by": req.answered_by,
                "answer_mode": req.answer_mode,
            },
            request_id=req.id,
            idem_key=f"briefing:{occurrence}:started",
        )
        rq.consume(con, req.id, actor=actor)
        return GateOutcome("start", req.id, occurrence, START_LINE, choice="now")

    if choice == "five":
        taken = snoozes + 1
        if taken > MAX_SNOOZES:
            publish(
                con,
                "briefing.skipped",
                actor,
                {
                    "schedule": schedule,
                    "occurrence": occurrence,
                    "reason": f"postponed {snoozes} times; not asking again today",
                    "snoozes": snoozes,
                },
                request_id=req.id,
                idem_key=f"briefing:{occurrence}:exhausted",
            )
            rq.consume(con, req.id, actor=actor)
            return GateOutcome("exhausted", req.id, occurrence, EXHAUSTED_LINE, choice="five")

        due_at = shift_ts(ts, SNOOZE_S)
        nxt = raise_gate(
            con,
            schedule=schedule,
            occurrence=occurrence,
            actor=actor,
            snoozes=taken,
            deliver_after=due_at,
            now_ts=ts,
        )
        publish(
            con,
            "briefing.snoozed",
            actor,
            {
                "schedule": schedule,
                "occurrence": occurrence,
                "snoozes": taken,
                "due_at": due_at,
                "next_request_id": nxt.id,
            },
            request_id=req.id,
            idem_key=f"briefing:{occurrence}:snoozed:{taken}",
        )
        rq.consume(con, req.id, actor=actor)
        return GateOutcome(
            "snoozed",
            req.id,
            occurrence,
            SNOOZE_LINE,
            choice="five",
            next_request=nxt,
            deliver_after=due_at,
        )

    if choice == "skip":
        # A timeout arrives here too: the presentation's default_answer IS "Skip
        # today", applied through the same compare-and-swap. answered_by tells
        # the two apart in the log, and nothing in this branch needs to.
        publish(
            con,
            "briefing.skipped",
            actor,
            {
                "schedule": schedule,
                "occurrence": occurrence,
                "reason": "skipped",
                "answered_by": req.answered_by,
                "answer_mode": req.answer_mode,
            },
            request_id=req.id,
            idem_key=f"briefing:{occurrence}:skipped",
        )
        rq.consume(con, req.id, actor=actor)
        return GateOutcome("skipped", req.id, occurrence, SKIP_LINE, choice="skip")

    publish(
        con,
        "briefing.unreadable",
        actor,
        {"schedule": schedule, "occurrence": occurrence, "answer": req.answer},
        request_id=req.id,
        idem_key=f"briefing:{occurrence}:unreadable:{req.id}",
    )
    rq.consume(con, req.id, actor=actor)
    return GateOutcome("unreadable", req.id, occurrence, UNREADABLE_LINE)
