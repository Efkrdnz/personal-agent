"""``/status``, ``/log today``, ``/spend``, ``/kill``, and "I'm going out".

Convenience, all of it. A channel's actual job is one Presentation and one
Answer; everything here is the operator asking the spine questions it can already
answer, formatted for a phone screen. None of these functions decides anything —
they read the spine's own words (``reconcile.project_status().lines``,
``ledger.spoken_status()``, ``presence`` reasons) rather than inventing a second
vocabulary that would drift from what Jarvis says out loud.

Two of them are not convenience at all:

``/kill`` goes through :func:`jarvis.kill.stop_everything`, which bumps the kill
epoch in the same transaction as the command row. The runner is a DIFFERENT OS
PROCESS holding a different connection, and it stops because it re-reads the
epoch — not because anything here reached into it. That is the only kill that
works from a phone, and it is the same code path the desk's "stop" uses.

"I'm going out" writes a presence OVERRIDE, which beats every sensor. The idle
probe on the desk machine cannot know the operator left the building, and being
wrong about that is what makes a question sit unasked for forty minutes.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC

from jarvis import jobs, kill, ledger, presence
from jarvis.clock import spoken_time, to_local
from jarvis.ids import now
from jarvis.reconcile import project_status

__all__ = [
    "HELP",
    "LOG_LIMIT",
    "Reply",
    "handle_command",
    "kill_switch",
    "log_today",
    "presence_override",
    "spend",
    "status",
]

LOG_LIMIT = 40

HELP = "\n".join(
    (
        "/status — what the machine is doing",
        "/log today — the activity log since midnight",
        "/spend — the ledger, including the meters that are not dollars",
        "/kill — stop everything, now",
        "",
        'Say "I\'m going out" or "I\'m back" to move presence by hand.',
    )
)


@dataclass(frozen=True, slots=True)
class Reply:
    """Text to send back. No keyboard: none of these asks a question."""

    text: str


def handle_command(
    con: sqlite3.Connection,
    text: str,
    *,
    chat_id: int,
    now_ts: str | None = None,
) -> Reply | None:
    """Dispatch one message. None means "this was not a command".

    Returning None rather than a "sorry?" is what lets the caller try the message
    as a free-text ANSWER to an open question. A channel that swallowed every
    unrecognised message would make "none of these" unreachable.
    """
    normalised = _norm(text)
    if not normalised:
        return None
    actor = f"telegram:{chat_id}"

    if normalised in ("/start", "/help", "help"):
        return Reply(HELP)
    if normalised.startswith("/status"):
        return Reply(status(con, now_ts=now_ts))
    if normalised.startswith("/log"):
        return Reply(log_today(con, now_ts=now_ts))
    if normalised.startswith("/spend"):
        return Reply(spend(con))
    if normalised.startswith("/kill"):
        return Reply(kill_switch(con, issued_by=actor))

    override = _override_phrase(normalised)
    if override is not None:
        return Reply(presence_override(con, override[0], by=actor))
    return None


# ───────────────────────────── /status ─────────────────────────────


def status(con: sqlite3.Connection, *, now_ts: str | None = None) -> str:
    """Running jobs, then the briefing's own project-status lines.

    ``project_status`` is read WITHOUT advancing the briefing cursor, which is
    exactly what it promises: asking on a phone must not make the morning
    briefing skip what it was going to say.
    """
    running = jobs.running(con)
    lines: list[str] = []
    if running:
        lines.append(f"Running ({len(running)}):")
        lines.extend(f"  • {j.title} — {j.state}" for j in running)
    else:
        lines.append("Nothing is running.")

    report = project_status(con, now_ts=now_ts)
    if report.lines:
        lines.append("")
        lines.extend(report.lines)
    if report.open_requests:
        lines.append("")
        noun = "question" if report.open_requests == 1 else "questions"
        lines.append(f"{report.open_requests} open {noun} waiting for you.")
    return "\n".join(lines)


# ───────────────────────────── /log today ─────────────────────────────


def log_today(con: sqlite3.Connection, *, now_ts: str | None = None, limit: int = LOG_LIMIT) -> str:
    """The activity log since local midnight, newest last.

    "Today" is a local-time idea and the database is UTC, so the boundary is
    computed through :mod:`jarvis.clock` — the one place a timezone is allowed to
    appear. Comparing ``ts`` against a UTC midnight would silently show the wrong
    three hours of the morning.
    """
    start = _local_midnight_utc(now_ts or now())
    rows = con.execute(
        "SELECT ts, kind, actor FROM events WHERE ts >= ? ORDER BY seq DESC LIMIT ?",
        (start, int(limit)),
    ).fetchall()
    if not rows:
        return "Nothing in the log today."
    lines = [f"{spoken_time(str(r['ts']))}  {r['kind']}  ({r['actor']})" for r in reversed(rows)]
    return "\n".join(["Today:", *lines])


# ───────────────────────────── /spend ─────────────────────────────


def spend(con: sqlite3.Connection, window: ledger.Window = "today") -> str:
    """The spoken sentence, then EVERY meter — including the unpriced ones.

    The per-meter list is not decoration. Under a Max subscription the priced
    total is frequently 0.00 while the machine has worked for hours, and a reply
    that stopped at the dollar figure would be read as "today was free". The
    spoken line already says so; the list says by how much.
    """
    report = ledger.status(con, window)
    lines = [ledger.spoken_status(report)]
    if report.by_provider:
        lines.append("")
        for provider in sorted(report.by_provider):
            totals = report.by_provider[provider]
            lines.append(f"{provider}:")
            for meter in totals.meters:
                if meter.usd_equiv is None:
                    money = "not convertible to dollars"
                else:
                    money = f"${meter.usd_equiv:.2f}"
                lines.append(f"  • {meter.amount:g} {meter.unit} — {money}")
    return "\n".join(lines)


# ───────────────────────────── /kill ─────────────────────────────


def kill_switch(con: sqlite3.Connection, *, issued_by: str, reason: str = "telegram") -> str:
    """Stop everything. Works across processes because the EPOCH is in the file.

    Nothing here signals anything. ``stop_everything`` bumps ``kill_epoch`` and
    writes a ``stop_all`` command; every runner checks the epoch it started under
    and refuses to continue. That is why this works from a phone on a train while
    the runner is a detached process on a locked desktop.
    """
    command_id = kill.stop_everything(con, issued_by=issued_by, reason=reason)
    epoch = kill.current_epoch(con)
    doomed = kill.doomed_jobs(con)
    if not doomed:
        return f"Stop sent (epoch {epoch}). Nothing was running."
    titles = ", ".join(j.title for j in doomed)
    return f"Stop sent (epoch {epoch}, command {command_id}). Stopping: {titles}."


# ───────────────────────────── presence ─────────────────────────────

_OVERRIDE_PHRASES: tuple[tuple[tuple[str, ...], str | None], ...] = (
    (("i'm going out", "im going out", "going out", "/away", "i'm out", "im out"), "away"),
    (("i'm back", "im back", "back", "/back", "i'm home", "im home"), None),
    (("don't call me", "dont call me", "/dnd", "do not call me"), "dnd"),
    (("desk only", "/deskonly", "desk-only"), "desk_only"),
)


def _override_phrase(normalised: str) -> tuple[str | None] | None:
    for phrases, mode in _OVERRIDE_PHRASES:
        if normalised in phrases:
            # Wrapped in a tuple so that "clear the override" (mode=None) is
            # distinguishable from "no phrase matched".
            return (mode,)
    return None


def presence_override(con: sqlite3.Connection, mode: str | None, *, by: str) -> str:
    """Apply the override and read back what Jarvis now believes, in its words."""
    presence.set_override(con, mode, by=by)  # type: ignore[arg-type]
    state = presence.update_presence(con)
    if mode is None:
        return f"Override cleared. {state.reason}"
    return f"Noted. {state.reason}"


# ───────────────────────────── plumbing ─────────────────────────────


def _norm(text: str) -> str:
    """Casefold, collapse whitespace, and normalise the curly apostrophe.

    A phone keyboard types U+2019 and a laptop types U+0027, and "I'm back" that
    only works from one of them is the kind of bug that gets diagnosed as "the
    bot is ignoring me".
    """
    flat = unicodedata.normalize("NFKC", text or "").replace("’", "'")
    return " ".join(flat.split()).casefold()


def _local_midnight_utc(ts: str) -> str:
    """Midnight in the speaking zone, expressed the way the database stores time."""
    local = to_local(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    stamped = local.astimezone(UTC)
    return f"{stamped.strftime('%Y-%m-%dT%H:%M:%S')}.{stamped.microsecond // 1000:03d}Z"
