"""The four questions that make an assistant trustworthy rather than impressive.

"What are you doing?", "what did you do?", "what has this cost me?" and "can you
reach me?" — R5's honesty requirement, as four tools. None of them writes
anything, and every sentence they return is composed where the DATA is
(:mod:`jarvis.reconcile`, :mod:`jarvis.ledger`, :mod:`jarvis.presence`) rather
than here, so a wording change happens once and the briefing and the desk cannot
drift apart.

THE ANSWER IS NEVER "nothing, everything is fine" BY DEFAULT. A status tool that
falls back to reassurance when it cannot read something is worse than one that
fails, because the reassurance is indistinguishable from the truth. Each of these
says what it could not determine.
"""

from __future__ import annotations

from jarvis import jobs, ledger, presence, reconcile
from jarvis.ids import now, parse_ts
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import ALL_CHANNELS, Tool

__all__ = ["project_status", "spend", "reachability", "TOOLS"]


def _humanised_jobs(running: list[jobs.Job]) -> str:
    titles = [j.title for j in running]
    if len(titles) == 1:
        return titles[0]
    return ", ".join(titles[:-1]) + f" and {titles[-1]}"


def project_status(ctx: ToolCtx, window_hours: float = 24.0) -> str:
    """What is running now, and what happened in the window the user asked about.

    ``window_hours`` is a parameter rather than the briefing cursor because this
    is asked at arbitrary times: "what did you do today" and "what did you do
    this week" are the same question over different windows, and reading the
    briefing's cursor here would silently advance nothing but would report a
    window nobody asked for.
    """
    hours = max(0.25, min(float(window_hours or 24.0), 24.0 * 30))
    since = jobs.shift_ts(now(), -hours * 3600)
    status = reconcile.project_status(ctx.con, since=since)

    live = jobs.running(ctx.con)
    parts: list[str] = []
    if live:
        parts.append(f"Right now: {_humanised_jobs(live)}.")

    if status.quiet and not status.open_requests:
        # `status.lines` falls back to "…since the last briefing", which is the
        # right sentence at ten in the morning and a false one here: this window
        # was chosen by the question, not by the cursor. Naming the window is
        # also what makes "nothing happened" distinguishable from "I didn't look".
        parts.append(f"Nothing finished, failed or got stuck in the last {_window(hours)}.")
    else:
        # Every other line is composed by `reconcile` next to the rows it
        # describes — including the "still waiting" count — so this tool and the
        # morning briefing cannot end up wording the same fact two ways.
        parts.extend(status.lines)
    return " ".join(parts)


def _window(hours: float) -> str:
    if hours <= 1.5:
        return "hour"
    if hours < 36:
        return f"{round(hours)} hours"
    return f"{round(hours / 24)} days"


def spend(ctx: ToolCtx, window: str = "today") -> str:
    """What this has cost, INCLUDING the meters that do not convert to money.

    :func:`jarvis.ledger.spoken_status` names every unpriced meter
    unconditionally. Under a Max subscription the dollar figure is frequently
    zero while the machine has been working for hours, and a tool that stopped at
    the dollars would be heard as "today was free".
    """
    allowed = ("today", "week", "month", "24h", "7d", "30d", "all")
    chosen = str(window or "today").strip().lower()
    if chosen not in allowed:
        return (
            f"I don't know the window '{window}'. Ask me about today, this week, "
            "this month, or all of it."
        )
    config = ledger.LedgerConfig(threshold_usd=_threshold(ctx))
    return ledger.spoken_status(ledger.status(ctx.con, chosen, config=config))  # type: ignore[arg-type]


def _threshold(ctx: ToolCtx) -> float:
    """The spoken ceiling, from whatever the composition root passed down.

    Read from ``ctx.extra`` rather than by importing :mod:`jarvis.config`: this
    layer must keep working in a process that was handed a threshold by a caller
    with no config file at all, and a tool that reads global configuration is a
    tool that behaves differently depending on who is running it.
    """
    try:
        return float(ctx.extra.get("spend_threshold_usd") or ledger.DEFAULT_CONFIG.threshold_usd)
    except (TypeError, ValueError):
        return ledger.DEFAULT_CONFIG.threshold_usd


def reachability(ctx: ToolCtx) -> str:
    """Where Jarvis believes it can reach the user, and how sure it is.

    The reason string is read back VERBATIM. It is the evidence the verdict rests
    on — "the screen has been locked for 40 minutes" — and paraphrasing it would
    turn a measurement into an opinion.
    """
    verdict = presence.evaluate_presence(ctx.con)
    where = ", ".join(verdict.reachable) if verdict.reachable else "nowhere"
    age = ""
    try:
        age = (
            f" — {presence.spoken_ago((parse_ts(now()) - parse_ts(verdict.since)).total_seconds())}"
        )
    except (TypeError, ValueError):
        age = ""
    return f"I think you're {verdict.state}{age}, and I can reach you on {where}. {verdict.reason}"


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="project_status",
        description=(
            "What Jarvis is working on now and what happened recently: builds that "
            "finished, failed, or are stuck waiting for an answer. Use this for 'what are "
            "you doing', 'what did you do today', 'how's the build going', 'anything "
            "waiting on me'. Not for starting work — that is build_project."
        ),
        handler=project_status,
        parameters={
            "type": "OBJECT",
            "properties": {
                "window_hours": {
                    "type": "NUMBER",
                    "description": (
                        "How far back to look. 24 for 'today', 168 for 'this week'. Defaults to 24."
                    ),
                }
            },
        },
        channels=ALL_CHANNELS,
    ),
    Tool(
        name="spend",
        description=(
            "What the assistant has cost over a window, in dollars where dollars are "
            "known and in named unpriced meters where they are not. Use this for 'what "
            "have I spent', 'how much has this cost', 'am I near the limit'."
        ),
        handler=spend,
        parameters={
            "type": "OBJECT",
            "properties": {
                "window": {
                    "type": "STRING",
                    "enum": ["today", "week", "month", "24h", "7d", "30d", "all"],
                    "description": "Defaults to today.",
                }
            },
        },
        channels=ALL_CHANNELS,
    ),
    Tool(
        name="reachability",
        description=(
            "Whether Jarvis believes the user is at the desk and which channels can "
            "reach them right now. Use this for 'can you hear me', 'where do you think I "
            "am', 'how would you reach me'."
        ),
        handler=reachability,
        parameters={"type": "OBJECT", "properties": {}},
        channels=ALL_CHANNELS,
    ),
)
