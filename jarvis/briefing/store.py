"""The rows: one briefing, its sections, its cursors, and what it has already said.

THE POINTER IS A ROW. ``briefings.position`` is where the briefing has got to,
and it is here rather than in the desk process or in a Telegram keyboard because
the channel is the thing most likely to disappear halfway through. A pointer held
by the channel means a dropped connection either restarts the briefing or loses
the rest of it; a pointer in SQLite means whichever process attaches next reads
section three.

Every function takes an open connection first and holds none. Two processes may
be doing this at once — the desk saying it, the Telegram bot answering it, the
scheduler starting tomorrow's — so each write that decides something is a
compare-and-swap and the loser is told it lost.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from jarvis.briefing.sections import SectionContent
from jarvis.clock import to_local
from jarvis.db import tx
from jarvis.ids import nid, now
from jarvis.reconcile import BRIEFING_CURSOR, set_briefing_cursor

__all__ = [
    "SEEN_KEEP_DAYS",
    "Briefing",
    "BriefingState",
    "SectionRow",
    "SectionState",
    "advance_cursor",
    "briefing_by_run_key",
    "cursor_of",
    "finish",
    "forget_older_than",
    "get",
    "mark_delivered",
    "mark_skipped",
    "remember",
    "run_key_for",
    "save_content",
    "section_at",
    "section_for_request",
    "sections",
    "seen_ids",
    "set_position",
    "start",
]

BriefingState = Literal["running", "finished", "stopped"]
SectionState = Literal["pending", "delivered", "skipped"]

#: How long an item stays in the exactness set. A month is far longer than any
#: cursor's own reach (Gmail keeps about a week of history, the GitHub search
#: window is a day) and short enough that the table stays small forever.
SEEN_KEEP_DAYS = 30


@dataclass(frozen=True, slots=True)
class Briefing:
    """One run, and the pointer into it."""

    id: str
    run_key: str
    state: BriefingState
    position: int
    created_at: str
    updated_at: str
    created_by: str
    finished_at: str | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True, slots=True)
class SectionRow:
    """One section of one run. ``content`` is present once it has been composed."""

    briefing_id: str
    position: int
    key: str
    state: SectionState
    content: SectionContent | None = None
    request_id: str | None = None
    composed_at: str | None = None
    delivered_at: str | None = None


def run_key_for(ts: str | None = None) -> str:
    """The identity of "this morning's briefing": the ISTANBUL date.

    Local, not UTC, and this is the one place in the briefing that cares: a
    briefing at 01:00 Istanbul is 22:00 UTC the previous day, and a UTC run key
    would file it under yesterday and then refuse to start today's. The
    conversion lives in :mod:`jarvis.clock`, which is the only module allowed to
    know the zone.
    """
    return to_local(ts or now()).strftime("%Y-%m-%d")


def _to_briefing(row: sqlite3.Row) -> Briefing:
    return Briefing(
        id=str(row["id"]),
        run_key=str(row["run_key"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        position=int(row["position"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        created_by=str(row["created_by"]),
        finished_at=row["finished_at"],
    )


def _to_section(row: sqlite3.Row) -> SectionRow:
    raw = row["content"]
    return SectionRow(
        briefing_id=str(row["briefing_id"]),
        position=int(row["position"]),
        key=str(row["key"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        content=SectionContent.from_dict(json.loads(raw)) if raw else None,
        request_id=row["request_id"],
        composed_at=row["composed_at"],
        delivered_at=row["delivered_at"],
    )


def start(
    con: sqlite3.Connection,
    keys: tuple[str, ...],
    *,
    actor: str,
    run_key: str | None = None,
    now_ts: str | None = None,
) -> Briefing:
    """Create this morning's briefing, or return the one that already exists.

    Idempotent through ``run_key``, and that is what makes the two scheduler
    failure modes harmless: a machine rebooted at 09:50 comes back up, runs
    whatever starts the briefing, and finds the row rather than starting a second
    briefing with its own pointer — which would be two voices reading different
    sections at the same person.

    BEGIN IMMEDIATE before the lookup, for the same reason
    :func:`jarvis.requests.create_request` does it: two processes starting at once
    must not both miss and both insert.
    """
    if not keys:
        raise ValueError("a briefing with no sections is not a briefing")
    if not actor:
        raise ValueError("actor is required: an unattributed briefing is unauditable")
    ts = now_ts or now()
    key = run_key or run_key_for(ts)
    with tx(con):
        row = con.execute("SELECT * FROM briefings WHERE run_key=?", (key,)).fetchone()
        if row is not None:
            return _to_briefing(row)
        bid = nid("brf")
        con.execute(
            """INSERT INTO briefings
                 (id, run_key, state, position, created_at, updated_at, created_by)
               VALUES (?,?, 'running', 0, ?, ?, ?)""",
            (bid, key, ts, ts, actor),
        )
        con.executemany(
            """INSERT INTO briefing_sections (briefing_id, position, key, state)
               VALUES (?,?,?, 'pending')""",
            [(bid, i, k) for i, k in enumerate(keys)],
        )
        row = con.execute("SELECT * FROM briefings WHERE id=?", (bid,)).fetchone()
    return _to_briefing(row)


def get(con: sqlite3.Connection, briefing_id: str) -> Briefing | None:
    row = con.execute("SELECT * FROM briefings WHERE id=?", (briefing_id,)).fetchone()
    return None if row is None else _to_briefing(row)


def briefing_by_run_key(con: sqlite3.Connection, run_key: str) -> Briefing | None:
    row = con.execute("SELECT * FROM briefings WHERE run_key=?", (run_key,)).fetchone()
    return None if row is None else _to_briefing(row)


def sections(con: sqlite3.Connection, briefing_id: str) -> tuple[SectionRow, ...]:
    rows = con.execute(
        "SELECT * FROM briefing_sections WHERE briefing_id=? ORDER BY position",
        (briefing_id,),
    ).fetchall()
    return tuple(_to_section(r) for r in rows)


def section_at(con: sqlite3.Connection, briefing_id: str, position: int) -> SectionRow | None:
    row = con.execute(
        "SELECT * FROM briefing_sections WHERE briefing_id=? AND position=?",
        (briefing_id, position),
    ).fetchone()
    return None if row is None else _to_section(row)


def section_for_request(con: sqlite3.Connection, request_id: str) -> SectionRow | None:
    """Which section this answered question belongs to.

    The reverse lookup is what lets an answer arriving on ANY channel be applied
    without the channel knowing anything about briefings: it hands over a request
    id, which is all it ever had.
    """
    row = con.execute(
        "SELECT * FROM briefing_sections WHERE request_id=?", (request_id,)
    ).fetchone()
    return None if row is None else _to_section(row)


def save_content(
    con: sqlite3.Connection,
    briefing_id: str,
    position: int,
    content: SectionContent,
    *,
    request_id: str | None = None,
) -> None:
    """Store the composed section and the request that carries it.

    Only ever called for a section that has not been composed yet — a second
    composition would make "repeat" say something different from what was heard
    the first time, which is the one thing "repeat" must not do.
    """
    con.execute(
        """UPDATE briefing_sections
              SET content=?, composed_at=?, request_id=COALESCE(?, request_id)
            WHERE briefing_id=? AND position=?""",
        (
            json.dumps(content.as_dict(), ensure_ascii=False),
            content.composed_at,
            request_id,
            briefing_id,
            position,
        ),
    )


def mark_delivered(
    con: sqlite3.Connection,
    briefing_id: str,
    position: int,
    *,
    now_ts: str | None = None,
) -> bool:
    """pending -> delivered, and COMMIT what saying it settled. False if already done.

    This is the only place a briefing cursor moves and the only place an item is
    remembered, and it runs in one transaction with the state flip so a crash
    cannot leave a section marked as said with its cursor still behind — or worse,
    the cursor moved for a section nobody heard.
    """
    ts = now_ts or now()
    with tx(con):
        row = con.execute(
            """UPDATE briefing_sections SET state='delivered', delivered_at=?
                WHERE briefing_id=? AND position=? AND state='pending'
            RETURNING content""",
            (ts, briefing_id, position),
        ).fetchone()
        if row is None:
            return False
        content = SectionContent.from_dict(json.loads(row["content"])) if row["content"] else None
        if content is not None and content.ok:
            remember(con, content.cursor_name or content.key, content.item_ids, now_ts=ts)
            if content.cursor_name and content.next_cursor:
                advance_cursor(con, content.cursor_name, content.next_cursor)
        _touch(con, briefing_id, ts)
    return True


def mark_skipped(
    con: sqlite3.Connection,
    briefing_id: str,
    position: int,
    *,
    now_ts: str | None = None,
) -> bool:
    """pending -> skipped. NOTHING is committed: a skipped section was not heard.

    "Skip to inbox" therefore leaves the skipped sections' cursors exactly where
    they were, and tomorrow says what today's skip passed over. Silently moving a
    cursor here is how "skip" would quietly become "discard".
    """
    ts = now_ts or now()
    with tx(con):
        row = con.execute(
            """UPDATE briefing_sections SET state='skipped'
                WHERE briefing_id=? AND position=? AND state='pending' RETURNING position""",
            (briefing_id, position),
        ).fetchone()
        if row is None:
            return False
        _touch(con, briefing_id, ts)
    return True


def set_position(
    con: sqlite3.Connection,
    briefing_id: str,
    position: int,
    *,
    expect: int | None = None,
) -> bool:
    """Move the pointer. With ``expect``, only if it has not already moved.

    The compare-and-swap is what makes a duplicated poke harmless: two processes
    both applying the same "next" produce one move, and the loser is told rather
    than skipping a section.
    """
    if expect is None:
        row = con.execute(
            "UPDATE briefings SET position=?, updated_at=? WHERE id=? AND state='running'"
            " RETURNING id",
            (position, now(), briefing_id),
        ).fetchone()
        return row is not None
    row = con.execute(
        "UPDATE briefings SET position=?, updated_at=?"
        " WHERE id=? AND state='running' AND position=? RETURNING id",
        (position, now(), briefing_id, expect),
    ).fetchone()
    return row is not None


def finish(
    con: sqlite3.Connection,
    briefing_id: str,
    state: BriefingState = "finished",
    *,
    now_ts: str | None = None,
) -> bool:
    """running -> finished|stopped. False if somebody already ended it."""
    if state == "running":
        raise ValueError("finish() ends a briefing; use set_position to move within one")
    ts = now_ts or now()
    row = con.execute(
        "UPDATE briefings SET state=?, finished_at=?, updated_at=?"
        " WHERE id=? AND state='running' RETURNING id",
        (state, ts, ts, briefing_id),
    ).fetchone()
    return row is not None


def _touch(con: sqlite3.Connection, briefing_id: str, ts: str) -> None:
    con.execute("UPDATE briefings SET updated_at=? WHERE id=?", (ts, briefing_id))


# ───────────────────────────── cursors and the seen set ─────────────────────────────


def cursor_of(con: sqlite3.Connection, name: str) -> str | None:
    row = con.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
    return None if row is None else str(row["value"])


def _is_forward(current: str, value: str) -> bool:
    """Is ``value`` later than ``current``, in whatever order this cursor counts in?

    NOT ALL CURSORS SORT AS TEXT, and one of them is the one most likely to
    break: Gmail's ``historyId`` is a decimal integer that grows without bound, so
    the morning it goes from ``9999999`` to ``10000001`` a lexicographic ``<=``
    calls the new id *older* and pins the cursor forever. The symptom is silent —
    the delta re-reads a widening window every day and then, a week later, the
    stored id ages out and every morning is a full resync — which is why the
    comparison asks what the values ARE rather than assuming they are timestamps.

    Timestamps take the text branch, and must: :func:`jarvis.ids.now` is
    fixed-width UTC precisely so that string order is chronological order.
    """
    if current.isdigit() and value.isdigit():
        return int(value) > int(current)
    return value > current


def advance_cursor(con: sqlite3.Connection, name: str, value: str) -> str:
    """Move one cursor forward. NEVER backwards, and never for a section nobody heard.

    Monotonic because the sources are not: a GitHub search whose index lagged, or
    a Gmail resync that read an older page, can hand back a cursor behind the
    stored one, and storing it would re-read a window whose items are already in
    the seen set — harmless the first time and an ever-growing query after that.
    :func:`_is_forward` decides what "behind" means, because one of these cursors
    is a number and the rest are timestamps.

    The projects cursor is written through
    :func:`jarvis.reconcile.set_briefing_cursor` rather than by the UPSERT below,
    because that function is where the decision to make this cursor INCLUSIVE is
    documented, and a second writer would be a second place for that decision to
    be changed by accident.
    """
    current = cursor_of(con, name)
    if current is not None and not _is_forward(current, value):
        return current
    if name == BRIEFING_CURSOR:
        return set_briefing_cursor(con, ts=value)
    con.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (name, value, now()),
    )
    return value


def seen_ids(con: sqlite3.Connection, source: str, ids: tuple[str, ...] = ()) -> set[str]:
    """Which of these have already been said. All of them for this source if ``ids`` is empty.

    Both shapes exist because both callers are right: composition wants the whole
    set for one source (the candidates are not known until the source has
    answered), while a caller holding ids wants to ask about exactly those. The
    set is scoped to one source and pruned to :data:`SEEN_KEEP_DAYS`, so neither
    is expensive.
    """
    if not ids:
        rows = con.execute("SELECT item_id FROM briefing_seen WHERE source=?", (source,)).fetchall()
        return {str(r["item_id"]) for r in rows}
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT item_id FROM briefing_seen WHERE source=? AND item_id IN ({marks})",
        (source, *ids),
    ).fetchall()
    return {str(r["item_id"]) for r in rows}


def remember(
    con: sqlite3.Connection,
    source: str,
    ids: tuple[str, ...],
    *,
    now_ts: str | None = None,
) -> int:
    """Record that these were said. Idempotent; returns how many were new."""
    if not ids:
        return 0
    ts = now_ts or now()
    before = len(seen_ids(con, source, ids))
    con.executemany(
        "INSERT INTO briefing_seen (source, item_id, first_seen_at) VALUES (?,?,?)"
        " ON CONFLICT(source, item_id) DO NOTHING",
        [(source, i, ts) for i in ids],
    )
    return len(ids) - before


def forget_older_than(con: sqlite3.Connection, before_ts: str) -> int:
    """Prune the exactness set. Safe because every cursor's own reach is shorter.

    Run by whatever maintenance pass exists; not by the briefing, which must not
    spend its morning doing housekeeping.
    """
    cur = con.execute("DELETE FROM briefing_seen WHERE first_seen_at < ?", (before_ts,))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
