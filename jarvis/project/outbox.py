"""The outbox rows this layer writes, and the at-most-once rule that owns them.

``outbox.at_most_once`` is commented in the frozen schema as "1 for phone.dial.
NEVER auto-retried." Repository creation belongs in that category for the same
reason a phone call does: the failure that matters is not "it did not happen", it
is "it happened twice and nobody knows which one is live". Four repositories
called comment-watcher, comment-watcher-1, comment-watcher-2 and
comment-watcher-3 is not a recoverable state; it is an account nobody trusts
again.

So the lifecycle of an at-most-once row is deliberately not the lifecycle of a
retryable one:

    pending --begin()--> inflight --done()------> done
                                 \\--refused()---> failed        (provably nothing happened)
                                  \\--escalate()-> needs_human   (nobody can know)

* :func:`begin` moves the row to ``inflight`` BEFORE the call goes out, in one
  compare-and-swap whose predicate includes ``attempts=0``. A second process, a
  restarted process, or a hand-written repair script that flips the row back to
  ``pending`` all hit the same wall: the attempt counter has moved, so the row is
  never attempted twice.
* a crash between ``begin`` and the outcome leaves an ``inflight`` row with an
  expired lease. That is NOT swept back to pending. :func:`stranded` reports it
  and :func:`escalate_stranded` moves it to ``needs_human``, because a process
  that died mid-create may well have created the repository.
* ``failed`` is reserved for a refusal GitHub actually gave us. "The name was
  taken" and "the token has no scope" provably changed nothing, so they are not
  ambiguous and do not need a human.

WHY THIS IS NOT IN THE SPINE YET. It writes one table with one op, and the
generic version wants the other ops (``speak``, ``telegram.send``,
``phone.dial``) in front of it before their shapes are guessed. When the second
op arrives, this moves to ``jarvis/outbox.py`` unchanged in behaviour — nothing
here knows what a repository is.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from jarvis.db import tx
from jarvis.ids import nid, now, parse_ts

__all__ = [
    "REPO_CREATE_OP",
    "OutboxRow",
    "OutboxState",
    "begin",
    "done",
    "enqueue",
    "escalate",
    "escalate_stranded",
    "get",
    "needs_human",
    "pending",
    "refused",
    "spoken_escalation",
    "stranded",
]

OutboxState = Literal["pending", "claimed", "inflight", "done", "failed", "needs_human"]

#: The one op this module is used for today. Named here so the idempotency key
#: and the dispatch cannot drift apart across processes.
REPO_CREATE_OP = "github.repo_create"

#: How long a claim on an at-most-once row is trusted before a human is told.
#: Deliberately generous: a slow create is not a lost create, and escalating a
#: call that is still in flight would make somebody look at a healthy account.
DEFAULT_LEASE_S = 120.0


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One row of ``outbox``, with the JSON columns parsed."""

    id: str
    ts: str
    due_at: str
    op: str
    args: dict[str, Any]
    idem_key: str
    state: OutboxState
    at_most_once: bool
    attempts: int
    max_attempts: int
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    effect_id: str | None = None

    @property
    def spent(self) -> bool:
        """True once this row may never be attempted again.

        For an at-most-once row that is the moment it was first attempted, which
        is the whole difference between this and a retry queue.
        """
        return self.state != "pending" or (self.at_most_once and self.attempts > 0)


def enqueue(
    con: sqlite3.Connection,
    *,
    op: str,
    args: dict[str, Any],
    idem_key: str,
    at_most_once: bool = True,
    max_attempts: int = 1,
    due_at: str | None = None,
) -> OutboxRow:
    """Append one row, or return the row this key already has.

    Idempotent by ``idem_key`` because the confirmation that authorised this can
    be consumed twice by a resumed process, and "asked twice, enqueued once" is
    the only safe reading of that. The key is the caller's: for a repository it
    is the full name, so two processes proposing the same repository converge on
    one row instead of two.
    """
    if at_most_once and max_attempts != 1:
        raise ValueError(
            "an at_most_once row may not have a retry budget: max_attempts must be 1, "
            f"got {max_attempts}"
        )
    if max_attempts < 1:
        raise ValueError("max_attempts is 1-based")
    if not idem_key:
        raise ValueError("an outbox row with no idem_key can be written twice")

    ts = now()
    # BEGIN IMMEDIATE around the lookup and the insert: two processes racing here
    # would otherwise both miss and one would die on the UNIQUE index instead of
    # finding the row that already authorises the side effect.
    with tx(con):
        found = con.execute("SELECT * FROM outbox WHERE idem_key=?", (idem_key,)).fetchone()
        if found is not None:
            return _row(found)
        oid = nid("ob")
        con.execute(
            """INSERT INTO outbox (id, ts, due_at, op, args, idem_key, state, at_most_once,
                                   attempts, max_attempts)
               VALUES (?,?,?,?,?,?,'pending',?,0,?)""",
            (
                oid,
                ts,
                due_at or ts,
                op,
                json.dumps(args, ensure_ascii=False, separators=(",", ":")),
                idem_key,
                1 if at_most_once else 0,
                max_attempts,
            ),
        )
        fresh = con.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
    return _row(fresh)


def get(con: sqlite3.Connection, row_id: str) -> OutboxRow | None:
    row = con.execute("SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()
    return _row(row) if row is not None else None


def pending(
    con: sqlite3.Connection, *, op: str | None = None, now_ts: str | None = None
) -> list[OutboxRow]:
    """Rows that are due and have never been attempted. Oldest first."""
    sql = "SELECT * FROM outbox WHERE state='pending' AND due_at <= ?"
    args: list[Any] = [now_ts or now()]
    if op is not None:
        sql += " AND op=?"
        args.append(op)
    sql += " ORDER BY due_at, rowid"
    return [_row(r) for r in con.execute(sql, args)]


def begin(
    con: sqlite3.Connection,
    row_id: str,
    claimed_by: str,
    *,
    lease_s: float = DEFAULT_LEASE_S,
) -> bool:
    """Take the row ``inflight`` before anything leaves the machine. CAS; False if spent.

    ``attempts=0`` is in the predicate for at-most-once rows on purpose: the
    state column alone would let a well-meaning "reset it to pending and try
    again" create a second repository, and this is the one place that can refuse
    it. The counter is bumped inside the same statement, so two connections
    racing produce exactly one winner.
    """
    ts = now()
    row = con.execute(
        """UPDATE outbox
              SET state='inflight', attempts=attempts+1, claimed_by=?, claim_expires_at=?
            WHERE id=? AND state='pending'
              AND (at_most_once=0 OR attempts=0)
              AND attempts < max_attempts
        RETURNING id""",
        (claimed_by, _plus(ts, lease_s), row_id),
    ).fetchone()
    return row is not None


def done(
    con: sqlite3.Connection,
    row_id: str,
    *,
    result: dict[str, Any],
    effect_id: str | None = None,
) -> bool:
    """The side effect happened and we know it. Links the effect row that records it."""
    row = con.execute(
        """UPDATE outbox SET state='done', result=?, error=NULL, effect_id=?,
                             claimed_by=NULL, claim_expires_at=NULL
            WHERE id=? AND state='inflight' RETURNING id""",
        (json.dumps(result, ensure_ascii=False, separators=(",", ":")), effect_id, row_id),
    ).fetchone()
    return row is not None


def refused(con: sqlite3.Connection, row_id: str, error: str) -> bool:
    """GitHub answered no. Provably nothing happened, so no human is needed."""
    return _terminate(con, row_id, "failed", error)


def escalate(con: sqlite3.Connection, row_id: str, error: str) -> bool:
    """Nobody can know whether it happened. A human looks; nothing retries."""
    return _terminate(con, row_id, "needs_human", error)


def stranded(
    con: sqlite3.Connection, *, now_ts: str | None = None, op: str | None = None
) -> list[OutboxRow]:
    """At-most-once rows whose claim lapsed while inflight: a process died mid-call.

    Reported rather than swept, for exactly the reason
    :func:`jarvis.effects.stale_undo_claims` is: the side effect may have reached
    the network before the crash.
    """
    sql = """SELECT * FROM outbox
              WHERE state='inflight' AND at_most_once=1
                AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?"""
    args: list[Any] = [now_ts or now()]
    if op is not None:
        sql += " AND op=?"
        args.append(op)
    sql += " ORDER BY claim_expires_at"
    return [_row(r) for r in con.execute(sql, args)]


def escalate_stranded(
    con: sqlite3.Connection, *, now_ts: str | None = None, op: str | None = None
) -> list[OutboxRow]:
    """Move every stranded row to ``needs_human`` and return the ones moved."""
    moved: list[OutboxRow] = []
    for row in stranded(con, now_ts=now_ts, op=op):
        if _terminate(
            con,
            row.id,
            "needs_human",
            f"the process holding this row died mid-call (attempt {row.attempts})",
            from_states=("inflight",),
        ):
            got = get(con, row.id)
            if got is not None:
                moved.append(got)
    return moved


def needs_human(con: sqlite3.Connection, *, op: str | None = None) -> list[OutboxRow]:
    """Everything waiting on a human. Feeds the morning briefing."""
    sql = "SELECT * FROM outbox WHERE state='needs_human'"
    args: list[Any] = []
    if op is not None:
        sql += " AND op=?"
        args.append(op)
    sql += " ORDER BY ts"
    return [_row(r) for r in con.execute(sql, args)]


def spoken_escalation(row: OutboxRow) -> str:
    """What Jarvis says about a row that needs a human. It never says "I'll retry".

    The sentence has to carry three things or it is useless: what was asked for,
    that the answer is unknown, and that nothing will happen on its own.
    """
    what = row.args.get("full_name") or row.args.get("name") or row.op
    if row.op != REPO_CREATE_OP:
        # One op, one sentence. A second op would need its own words, and a
        # generic "something went wrong" is exactly the escalation nobody acts on.
        return (
            f"I started {row.op} for {what} and never got an answer, so I don't know whether it "
            "happened. I am not going to try again on my own — have a look and tell me what you "
            "see."
        )
    return (
        f"I asked GitHub to create {what} and never got an answer, so I don't know whether it "
        "exists. I am not going to ask again on my own — a second attempt could make two "
        "repositories. Have a look at the account and tell me what you see."
    )


# ───────────────────────────── plumbing ─────────────────────────────


def _terminate(
    con: sqlite3.Connection,
    row_id: str,
    state: OutboxState,
    error: str,
    *,
    from_states: tuple[str, ...] = ("pending", "inflight", "claimed"),
) -> bool:
    marks = ",".join("?" * len(from_states))
    row = con.execute(
        f"""UPDATE outbox SET state=?, error=?, claimed_by=NULL, claim_expires_at=NULL
             WHERE id=? AND state IN ({marks}) RETURNING id""",
        (state, error, row_id, *from_states),
    ).fetchone()
    return row is not None


def _plus(ts: str, seconds: float) -> str:
    """``ts`` shifted, in exactly :func:`jarvis.ids.now`'s fixed-width format.

    These strings are compared lexicographically in SQL, so a microsecond-width
    timestamp would sort wrong against every other row and a lease would never
    look expired.
    """
    t = parse_ts(ts) + timedelta(seconds=seconds)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"


def _row(row: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=str(row["id"]),
        ts=str(row["ts"]),
        due_at=str(row["due_at"]),
        op=str(row["op"]),
        args=json.loads(row["args"]),
        idem_key=str(row["idem_key"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        at_most_once=bool(row["at_most_once"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        claimed_by=row["claimed_by"],
        claim_expires_at=row["claim_expires_at"],
        result=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
        effect_id=row["effect_id"],
    )
