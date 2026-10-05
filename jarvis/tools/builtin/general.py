"""The everyday tools: remember this, remind me then, look that up.

What makes a voice assistant general rather than a build console. Each one is
small, and each one answers in a sentence that says exactly what was stored or
found — "I'll remind you at 18:00 today", never "done" — because the user
cannot see a confirmation screen and the time they hear is the one that counts.

THE SEARCH IS HANDED IN. ``ctx.extra["search"]`` is a ``(query) -> str``
built by the composition root from the Gemini key (a grounded Google Search
call, see :class:`jarvis.live.text.GeminiSearch`). This module never learns
that Gemini exists, and a channel with no key gets a sentence saying so.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis import memory
from jarvis.clock import day_month, local_tz, spoken_date
from jarvis.ids import parse_ts
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Tool, ToolError

__all__ = [
    "SEARCH",
    "TOOLS",
    "TZ",
    "cancel_reminder",
    "forget_note",
    "list_reminders",
    "recall",
    "remember",
    "remind_me",
    "web_search",
]

#: Keys in :attr:`ToolCtx.extra`, set by the composition root.
SEARCH = "search"
TZ = "tz"


def _zone(ctx: ToolCtx) -> ZoneInfo:
    name = ctx.extra.get(TZ)
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return local_tz()


def _when(ts: str, zone: ZoneInfo, today: datetime) -> str:
    """'18:00 today', '09:00 tomorrow', '10:00 on Friday 9 October'."""
    local = parse_ts(ts).astimezone(zone)
    days = (local.date() - today.date()).days
    clock = local.strftime("%H:%M")
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{clock} on {day_month(local)}"


# ───────────────────────────── memory ─────────────────────────────


def remember(ctx: ToolCtx, fact: str) -> str:
    try:
        memory.remember(ctx.con, fact, actor=ctx.actor, channel=ctx.channel)
    except ValueError as exc:
        raise ToolError("What should I remember?") from exc
    return f"I'll remember that: {' '.join(fact.split())}"


def recall(ctx: ToolCtx, about: str = "") -> str:
    found = memory.recall(ctx.con, about, limit=5)
    if not found:
        return (
            f"You haven't told me anything about {about}."
            if about
            else "You haven't told me anything to remember yet."
        )
    lines = [f"{n.text} (noted {spoken_date(n.created_at)})" for n in found]
    return "Here's what you told me: " + "; ".join(lines) + "."


def forget_note(ctx: ToolCtx, about: str) -> str:
    gone = memory.forget(ctx.con, about)
    if not gone:
        raise ToolError(f"I don't have a note about {about}.")
    return "Forgotten: " + "; ".join(n.text for n in gone) + "."


# ───────────────────────────── reminders ─────────────────────────────


def remind_me(ctx: ToolCtx, what: str, when: str) -> str:
    zone = _zone(ctx)
    now_local = datetime.now(zone)
    due = memory.parse_when(when, now_local)
    if due is None:
        raise ToolError(
            f"I didn't catch when — '{when}'. Try 'in 20 minutes', 'at 6pm' or 'tomorrow at 9'."
        )
    if due <= now_local:
        raise ToolError("That time has already passed.")
    try:
        r = memory.add_reminder(ctx.con, what, due, actor=ctx.actor, channel=ctx.channel)
    except ValueError as exc:
        raise ToolError("What should I remind you about?") from exc
    # The resolved time is said back, every time: "at 6" became 18:00 or 06:00
    # by a rule, and the user is the one who can tell whether the rule was right.
    return (
        f"I'll remind you to {r.text} at {_when(r.due_at, zone, now_local)}. "
        "The scheduler has to be running for me to say it."
    )


def list_reminders(ctx: ToolCtx) -> str:
    zone = _zone(ctx)
    pending = memory.pending_reminders(ctx.con)
    if not pending:
        return "You have no reminders set."
    today = datetime.now(zone)
    return (
        "Your reminders: "
        + "; ".join(f"{r.text} at {_when(r.due_at, zone, today)}" for r in pending[:8])
        + "."
    )


def cancel_reminder(ctx: ToolCtx, about: str) -> str:
    gone = memory.cancel_reminder(ctx.con, about)
    if not gone:
        raise ToolError(f"I don't have a reminder about {about}.")
    return "Cancelled: " + "; ".join(r.text for r in gone) + "."


# ───────────────────────────── the web ─────────────────────────────


def web_search(ctx: ToolCtx, query: str) -> str:
    search = ctx.extra.get(SEARCH)
    if not callable(search):
        raise ToolError(
            "I can't search the web from here — it needs the Gemini key "
            "(python -m jarvis secrets set gemini_api_key)."
        )
    q = " ".join(query.split())
    if not q:
        raise ToolError("What should I look up?")
    fn: Callable[[str], str] = search
    try:
        return fn(q)
    except Exception as exc:  # noqa: BLE001 - a dead search is a sentence, not a crash
        raise ToolError(f"The search didn't work: {exc}") from exc


_STR = {"type": "STRING"}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="remember",
        description=(
            "Store a fact the user asks you to remember: 'remember my locker is 214', 'note "
            "that Ali's birthday is in May'. fact is the user's own sentence, unparaphrased."
        ),
        handler=remember,
        parameters={"type": "OBJECT", "properties": {"fact": _STR}, "required": ["fact"]},
    ),
    Tool(
        name="recall",
        description=(
            "Look up what the user asked you to remember: 'what's my locker number', 'what "
            "did I tell you about Ali'. about is a few key words; empty lists recent notes."
        ),
        handler=recall,
        parameters={"type": "OBJECT", "properties": {"about": _STR}},
    ),
    Tool(
        name="forget_note",
        description="Forget a remembered fact: 'forget the locker thing'. about is key words.",
        handler=forget_note,
        parameters={"type": "OBJECT", "properties": {"about": _STR}, "required": ["about"]},
    ),
    Tool(
        name="remind_me",
        description=(
            "Set a reminder. what is the thing to be reminded of ('call mum'); when is the "
            "time AS THE USER SAID IT ('in 20 minutes', 'at 6pm', 'tomorrow at 9', 'on "
            "friday at 10'). Do not convert the time yourself; say back the time the tool "
            "returns."
        ),
        handler=remind_me,
        parameters={
            "type": "OBJECT",
            "properties": {"what": _STR, "when": _STR},
            "required": ["what", "when"],
        },
    ),
    Tool(
        name="list_reminders",
        description="Say which reminders are set: 'what are my reminders'.",
        handler=list_reminders,
    ),
    Tool(
        name="cancel_reminder",
        description="Cancel a reminder: 'cancel the mum reminder'. about is key words.",
        handler=cancel_reminder,
        parameters={"type": "OBJECT", "properties": {"about": _STR}, "required": ["about"]},
    ),
    Tool(
        name="web_search",
        description=(
            "Search the web for anything current or factual you are not sure of: news, "
            "scores, prices, opening hours, 'who is', 'what happened'. Not for the weather "
            "(use weather) or the time (use local_time)."
        ),
        handler=web_search,
        parameters={"type": "OBJECT", "properties": {"query": _STR}, "required": ["query"]},
    ),
)
