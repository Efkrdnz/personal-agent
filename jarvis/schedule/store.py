"""The schedules table: arm it, claim it, fire it once, advance it.

THE ONE THAT BITES is two daemons firing the same morning, and the answer is the
same compare-and-swap the rest of this system uses for answers and deliveries:
``UPDATE ... WHERE the claim is free RETURNING``. There is no lock and no leader.
The loser gets ``None`` and goes back to sleep.

WHY A LEASE AND NOT A STRAIGHT ADVANCE. Moving ``next_run_at`` to tomorrow in the
same statement that claims the row would be simpler and would lose the briefing
whenever the daemon died between claiming and asking — silently, once, at the one
moment nobody is watching. So the claim takes a short lease, the fire happens,
and :func:`complete` advances the pointer. A crash in between drops the lease and
the next tick tries again, still inside the grace window.

The fire itself cannot be inside the claim's transaction:
:func:`jarvis.requests.create_request` opens its own ``BEGIN IMMEDIATE`` and
SQLite has no nested transactions. That is why the lease exists rather than one
atomic claim-and-ask, and it is the first place this component did not get to
reuse the spine's shape exactly.

MISFIRES. Lateness is measured against the occurrence the fire is FOR — see
:func:`jarvis.schedule.recurrence.last_at_or_before` — not against the pointer,
so a machine that was off for three days delivers ONE late fire for today and
records the three it slept through. Past ``grace_s`` the fire is dropped and
``last_outcome='missed'`` says so; the alternative is a briefing read out at four
in the afternoon, which is not a late briefing but a wrong one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from jarvis.db import tx
from jarvis.ids import canon, nid, now, parse_ts
from jarvis.jobs import shift_ts
from jarvis.schedule.recurrence import (
    check_at_local,
    last_at_or_before,
    missed_occurrences,
    next_after,
)

__all__ = [
    "CLAIM_LEASE_S",
    "DEFAULT_GRACE_S",
    "Claim",
    "FireOutcome",
    "Schedule",
    "UnknownSchedule",
    "claim",
    "complete",
    "due",
    "ensure_schedule",
    "get_schedule",
    "list_schedules",
    "note_request",
    "release",
    "retime",
    "set_enabled",
]

#: One hour. Long enough that a reboot, a slow boot and a late network do not cost
#: the morning; short enough that nothing arrives at lunchtime calling itself a
#: morning briefing.
DEFAULT_GRACE_S = 3600

#: Two minutes: longer than any fire this system performs (one INSERT and one
#: event), short enough that a daemon killed mid-fire is retried well inside the
#: grace window.
CLAIM_LEASE_S = 120

FireOutcome = Literal["fired", "missed", "error"]


class UnknownSchedule(KeyError):
    """A schedule name nothing has ever armed."""


@dataclass(frozen=True, slots=True)
class Schedule:
    """One row of :file:`003_schedules.sql`."""

    id: str
    name: str
    fires: str
    payload: dict[str, Any]
    at_local: str
    tz: str
    enabled: bool
    grace_s: int
    next_run_at: str
    created_at: str
    updated_at: str
    last_due_at: str | None = None
    last_fired_at: str | None = None
    last_late_s: float | None = None
    last_outcome: str | None = None
    last_request_id: str | None = None
    fire_count: int = 0
    missed_count: int = 0
    claimed_by: str | None = None
    claim_expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    """The right to fire one occurrence once, held until the lease lapses."""

    schedule: Schedule
    claimed_by: str
    due_at: str
    fired_at: str
    late_s: float
    within_grace: bool
    missed: int

    @property
    def late(self) -> bool:
        """True when this fire is late enough that the log should say so."""
        return self.late_s >= 1.0


def ensure_schedule(
    con: sqlite3.Connection,
    *,
    name: str,
    fires: str,
    at_local: str,
    tz: str,
    payload: dict[str, Any] | None = None,
    grace_s: int = DEFAULT_GRACE_S,
    enabled: bool = True,
    now_ts: str | None = None,
) -> Schedule:
    """Arm this schedule if it is not armed, and return it either way.

    The daemon calls this at every startup, so it must NOT rewrite a row that
    already exists: re-arming on boot would silently undo a disable, and a
    restart loop would keep pushing ``next_run_at`` forward past the fire it was
    about to deliver. Changing the time on purpose is :func:`retime`.
    """
    check_at_local(at_local, tz)
    ts = now_ts or now()
    with tx(con):
        row = con.execute("SELECT * FROM schedules WHERE name=?", (name,)).fetchone()
        if row is None:
            con.execute(
                """INSERT INTO schedules
                     (id, name, fires, payload, at_local, tz, enabled, grace_s,
                      next_run_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    nid("sched"),
                    name,
                    fires,
                    canon(payload or {}),
                    at_local,
                    tz,
                    int(enabled),
                    int(grace_s),
                    next_after(ts, at_local, tz),
                    ts,
                    ts,
                ),
            )
            row = con.execute("SELECT * FROM schedules WHERE name=?", (name,)).fetchone()
    return _to_schedule(row)


def retime(
    con: sqlite3.Connection,
    name: str,
    *,
    at_local: str,
    tz: str,
    now_ts: str | None = None,
) -> Schedule:
    """Move a schedule to a different local time, re-arming it from now."""
    check_at_local(at_local, tz)
    ts = now_ts or now()
    row = con.execute(
        """UPDATE schedules SET at_local=?, tz=?, next_run_at=?, updated_at=?
            WHERE name=? RETURNING *""",
        (at_local, tz, next_after(ts, at_local, tz), ts, name),
    ).fetchone()
    if row is None:
        raise UnknownSchedule(name)
    return _to_schedule(row)


def set_enabled(
    con: sqlite3.Connection, name: str, enabled: bool, *, now_ts: str | None = None
) -> Schedule:
    """Enable or disable. Enabling re-arms from NOW rather than from the past.

    A schedule disabled for a fortnight has a pointer a fortnight old, and
    enabling it must not mean "and deliver the one you owe me from then".
    """
    ts = now_ts or now()
    current = get_schedule(con, name)
    if current is None:
        raise UnknownSchedule(name)
    next_run = next_after(ts, current.at_local, current.tz) if enabled else current.next_run_at
    row = con.execute(
        """UPDATE schedules SET enabled=?, next_run_at=?, claimed_by=NULL,
                                claim_expires_at=NULL, updated_at=?
            WHERE name=? RETURNING *""",
        (int(enabled), next_run, ts, name),
    ).fetchone()
    return _to_schedule(row)


def get_schedule(con: sqlite3.Connection, name: str) -> Schedule | None:
    row = con.execute("SELECT * FROM schedules WHERE name=?", (name,)).fetchone()
    return None if row is None else _to_schedule(row)


def list_schedules(con: sqlite3.Connection) -> list[Schedule]:
    rows = con.execute("SELECT * FROM schedules ORDER BY next_run_at, name").fetchall()
    return [_to_schedule(r) for r in rows]


def due(con: sqlite3.Connection, now_ts: str | None = None, limit: int = 50) -> list[Schedule]:
    """Enabled, due, and not held by a live claim. A dead claim reappears here."""
    ts = now_ts or now()
    rows = con.execute(
        """SELECT * FROM schedules
            WHERE enabled=1 AND next_run_at <= ?
              AND (claimed_by IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= ?)
            ORDER BY next_run_at LIMIT ?""",
        (ts, ts, limit),
    ).fetchall()
    return [_to_schedule(r) for r in rows]


def claim(
    con: sqlite3.Connection,
    schedule_id: str,
    claimed_by: str,
    *,
    now_ts: str | None = None,
    lease_s: int = CLAIM_LEASE_S,
) -> Claim | None:
    """Take the right to fire this schedule once. ``None`` means somebody else has it.

    The whole guard against two briefings is this one statement: the predicate
    re-checks due-ness and the claim inside the UPDATE, so two processes that
    both saw the row in :func:`due` produce exactly one winner.

    Raises ``ValueError`` — having given the lease back — when the row's
    ``at_local``/``tz`` pair is not one an occurrence can be computed from.
    """
    ts = now_ts or now()
    row = con.execute(
        """UPDATE schedules SET claimed_by=?, claim_expires_at=?, updated_at=?
            WHERE id=? AND enabled=1 AND next_run_at <= ?
              AND (claimed_by IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= ?)
        RETURNING *""",
        (claimed_by, shift_ts(ts, lease_s), ts, schedule_id, ts, ts),
    ).fetchone()
    if row is None:
        return None
    sched = _to_schedule(row)
    try:
        # Timestamps are fixed-width UTC, so max() on the strings is max() on the
        # instants. The later of the two is the honest occurrence: the pointer says
        # what is owed, the recurrence says which morning "now" belongs to, and after
        # an outage the recurrence is the one that has kept up.
        due_at = max(sched.next_run_at, last_at_or_before(ts, sched.at_local, sched.tz))
        missed = missed_occurrences(sched.next_run_at, due_at, sched.at_local, sched.tz)
    except ValueError:
        # The lease is already taken at this point, and a row whose zone is a
        # typo would otherwise sit claimed by a process that did nothing with it
        # — invisible for two minutes, then claimed again by the next one. The
        # ADR sells this table on being editable with the sqlite3 CLI at 2am, so
        # the edit that makes a row uncomputable has to give the lease straight
        # back and let the caller report it.
        con.execute(
            """UPDATE schedules SET claimed_by=NULL, claim_expires_at=NULL
                WHERE id=? AND claimed_by=?""",
            (sched.id, claimed_by),
        )
        raise
    late_s = (parse_ts(ts) - parse_ts(due_at)).total_seconds()
    return Claim(
        schedule=sched,
        claimed_by=claimed_by,
        due_at=due_at,
        fired_at=ts,
        late_s=late_s,
        within_grace=late_s <= sched.grace_s,
        missed=missed,
    )


def complete(
    con: sqlite3.Connection,
    held: Claim,
    *,
    outcome: FireOutcome,
    now_ts: str | None = None,
) -> Schedule | None:
    """Record what happened and re-arm. ``None`` if the claim was lost meanwhile.

    ``next_run_at`` is computed from NOW, never from the occurrence just handled.
    That is the no-catch-up rule in one line: a machine that slept through four
    mornings wakes up owing exactly one fire, not four.
    """
    ts = now_ts or now()
    sched = held.schedule
    row = con.execute(
        """UPDATE schedules
              SET next_run_at=?, last_due_at=?, last_fired_at=?, last_late_s=?,
                  last_outcome=?, fire_count=fire_count+?, missed_count=missed_count+?,
                  claimed_by=NULL, claim_expires_at=NULL, updated_at=?
            WHERE id=? AND claimed_by=? RETURNING *""",
        (
            next_after(ts, sched.at_local, sched.tz),
            held.due_at,
            ts,
            held.late_s,
            outcome,
            1 if outcome == "fired" else 0,
            held.missed + (1 if outcome == "missed" else 0),
            ts,
            sched.id,
            held.claimed_by,
        ),
    ).fetchone()
    return None if row is None else _to_schedule(row)


def release(con: sqlite3.Connection, held: Claim, *, now_ts: str | None = None) -> bool:
    """Give the claim back WITHOUT advancing the pointer, after a failed fire.

    The schedule stays due, so the next tick retries it — which is only correct
    because the fire itself is idempotent: the same occurrence hashes to the same
    ``dedupe_key`` and :func:`jarvis.requests.create_request` returns the row it
    already made.
    """
    ts = now_ts or now()
    row = con.execute(
        """UPDATE schedules SET claimed_by=NULL, claim_expires_at=NULL, updated_at=?
            WHERE id=? AND claimed_by=? RETURNING id""",
        (ts, held.schedule.id, held.claimed_by),
    ).fetchone()
    return row is not None


def note_request(
    con: sqlite3.Connection, schedule_id: str, request_id: str, *, now_ts: str | None = None
) -> bool:
    """Point the schedule at the request it just raised. THE SERVER-SIDE POINTER.

    Nothing about a briefing lives in the process that started it: the row here
    is how a daemon started after that one died finds the question it asked, and
    how an answer that arrived on Telegram is noticed by a desk that was asleep.
    """
    ts = now_ts or now()
    row = con.execute(
        "UPDATE schedules SET last_request_id=?, updated_at=? WHERE id=? RETURNING id",
        (request_id, ts, schedule_id),
    ).fetchone()
    return row is not None


def _to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        id=str(row["id"]),
        name=str(row["name"]),
        fires=str(row["fires"]),
        payload=json.loads(row["payload"]),
        at_local=str(row["at_local"]),
        tz=str(row["tz"]),
        enabled=bool(row["enabled"]),
        grace_s=int(row["grace_s"]),
        next_run_at=str(row["next_run_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_due_at=row["last_due_at"],
        last_fired_at=row["last_fired_at"],
        last_late_s=row["last_late_s"],
        last_outcome=row["last_outcome"],
        last_request_id=row["last_request_id"],
        fire_count=int(row["fire_count"]),
        missed_count=int(row["missed_count"]),
        claimed_by=row["claimed_by"],
        claim_expires_at=row["claim_expires_at"],
    )
