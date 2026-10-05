"""Which processes are running right now, as rows any other process can read.

THE WINDOW CANNOT ASK THE DESK. They are two OS processes with no parent in
common, no socket between them and no object either could hold. So the desk
says what it is doing — asleep, awake, listening, speaking — by writing one row
twice a second, and the window reads that row. That is the architecture
everywhere in this tree: processes talk through rows, and a reader that starts
after the writer died still gets an honest answer.

WHY THE ``cursors`` TABLE AND NO MIGRATION. A heartbeat is a small durable value
that one process advances and several read, which is exactly what ``cursors``
already is (the spend latch lives there for the same reason). Rows are named
``alive.<process>`` and hold JSON.

ABSENCE IS THE SIGNAL FOR A CRASH. A process that dies without saying so stops
beating, and :func:`read` treats a row older than ``within_s`` as no row at all.
A process that exits cleanly calls :func:`gone`, so the window can show it as
offline at once instead of ten seconds later.

``since`` IS DECIDED INSIDE THE WRITE. It is "how long has the desk been
listening", so it must survive every beat that repeats the state and reset on
the one that changes it. Deciding that in Python — read the row, compare, write
— is a race the moment two connections beat the same name (a restarted desk
overlapping its predecessor). One UPSERT that compares against the row AS IT IS
AT WRITE TIME cannot lose that race, so the comparison lives in the SQL.

Spine: standard library only, and every function takes an open connection.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from jarvis.ids import now, parse_ts

__all__ = [
    "DESK_STATES",
    "OFFLINE",
    "PROCESSES",
    "STALE_S",
    "Beat",
    "beat",
    "gone",
    "key",
    "read",
    "read_all",
]

PROCESSES: tuple[str, ...] = ("desk", "schedule", "telegram", "window")
DESK_STATES: tuple[str, ...] = ("asleep", "awake", "listening", "speaking")

#: What :func:`gone` writes. Not a desk state: it is what any process says on
#: its way out, and the window renders it as "not running" without the wait.
OFFLINE = "offline"

#: A beat older than this is a process that stopped beating. Twenty missed
#: half-second desk beats is not jitter; it is a crash or a hang.
STALE_S = 10.0

_PREFIX = "alive."

# The previous beat's `since` is carried forward only when the previous beat is
# from the same pid, says the same state, and is itself fresh. Anything else —
# a state change, a restarted process that reused the name, a gap long enough
# that the old beat was stale — starts the clock again, because "listening for
# four minutes" must not quietly include two minutes when nobody was beating.
_UPSERT = """
INSERT INTO cursors (name, value, updated_at) VALUES (:name, :value, :at)
ON CONFLICT(name) DO UPDATE SET
  value = CASE
    WHEN json_valid(cursors.value) AND cursors.updated_at >= :fresh_after THEN
      CASE
        WHEN json_extract(cursors.value, '$.state') IS json_extract(excluded.value, '$.state')
         AND json_extract(cursors.value, '$.pid') IS json_extract(excluded.value, '$.pid')
         AND json_type(cursors.value, '$.since') = 'text'
        THEN json_set(excluded.value, '$.since', json_extract(cursors.value, '$.since'))
        ELSE excluded.value
      END
    ELSE excluded.value
  END,
  updated_at = excluded.updated_at
"""


@dataclass(frozen=True, slots=True)
class Beat:
    """One process's last word about itself."""

    process: str
    pid: int
    state: str | None
    detail: dict[str, Any]
    #: When ``state`` last changed, in :func:`jarvis.ids.now`'s shape.
    since: str
    #: When this beat was written.
    at: str
    #: Seconds between ``at`` and the moment of reading. Never negative: a
    #: writer whose clock runs slightly ahead is fresh, not from the future.
    age_s: float


def key(process: str) -> str:
    """The ``cursors`` row name for ``process``."""
    _check_process(process)
    return f"{_PREFIX}{process}"


def beat(
    con: sqlite3.Connection,
    process: str,
    *,
    state: str | None = None,
    detail: dict[str, Any] | None = None,
    now_ts: str | None = None,
) -> None:
    """Say "I am alive, and this is what I am doing". One atomic UPSERT.

    A desk state outside :data:`DESK_STATES` RAISES. The window branches on these
    four words, and a fifth one written here would leave the orb showing nothing
    while every test of the writer passed — the bug class this tree is prone to.
    """
    _check_process(process)
    if process == "desk" and state not in (*DESK_STATES, OFFLINE):
        raise ValueError(f"desk state must be one of {DESK_STATES}, got {state!r}")
    if detail is not None and not isinstance(detail, dict):
        raise TypeError(f"detail must be a dict, got {type(detail).__name__}")
    at = now_ts or now()
    value = json.dumps(
        {"pid": os.getpid(), "state": state, "detail": detail or {}, "since": at},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    con.execute(
        _UPSERT,
        {"name": key(process), "value": value, "at": at, "fresh_after": _shift(at, -STALE_S)},
    )


def gone(con: sqlite3.Connection, process: str, *, now_ts: str | None = None) -> None:
    """A clean exit: say "offline" now rather than going quiet for ten seconds."""
    beat(con, process, state=OFFLINE, now_ts=now_ts)


def read(
    con: sqlite3.Connection,
    process: str,
    *,
    within_s: float = STALE_S,
    now_ts: str | None = None,
) -> Beat | None:
    """The process's last beat, or None if there is none, it is garbage, or it is stale.

    An ``offline`` beat from :func:`gone` IS returned while it is fresh: "it
    said goodbye" and "it vanished" are different things to show.
    """
    row = con.execute(
        "SELECT value, updated_at FROM cursors WHERE name=?", (key(process),)
    ).fetchone()
    if row is None:
        return None
    return _to_beat(process, row[0], row[1], now_ts or now(), within_s)


def read_all(
    con: sqlite3.Connection,
    *,
    within_s: float = STALE_S,
    now_ts: str | None = None,
) -> dict[str, Beat | None]:
    """Every process in :data:`PROCESSES`, in one query. What a 250 ms poll wants."""
    names = [key(p) for p in PROCESSES]
    rows = con.execute(
        f"SELECT name, value, updated_at FROM cursors WHERE name IN ({','.join('?' * len(names))})",
        names,
    ).fetchall()
    found = {str(r[0]): (r[1], r[2]) for r in rows}
    ts = now_ts or now()
    out: dict[str, Beat | None] = {}
    for process in PROCESSES:
        hit = found.get(key(process))
        out[process] = None if hit is None else _to_beat(process, hit[0], hit[1], ts, within_s)
    return out


# ───────────────────────────── plumbing ─────────────────────────────


def _check_process(process: str) -> None:
    # A typo here ("scheduler" for "schedule") would write a row nobody reads
    # and the window would show the scheduler as dead forever. Refuse it loudly.
    if process not in PROCESSES:
        raise ValueError(f"unknown process {process!r}; expected one of {PROCESSES}")


def _to_beat(
    process: str, value: Any, updated_at: Any, now_ts: str, within_s: float
) -> Beat | None:
    """Parse one row, or None. A row five processes can write is never trusted."""
    try:
        body = json.loads(value)
        at = str(updated_at)
        age = (parse_ts(now_ts) - parse_ts(at)).total_seconds()
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or age > within_s:
        return None
    pid, state, detail, since = (body.get(k) for k in ("pid", "state", "detail", "since"))
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if state is not None and not isinstance(state, str):
        return None
    if not isinstance(since, str):
        since = at
    return Beat(
        process=process,
        pid=pid,
        state=state,
        detail=detail if isinstance(detail, dict) else {},
        since=since,
        at=at,
        age_s=max(0.0, age),
    )


def _shift(ts: str, seconds: float) -> str:
    """``ts`` moved by ``seconds`` in the exact fixed-width shape the column sorts by.

    Not :func:`jarvis.jobs.shift_ts`: that module reads ``/proc`` and signals
    processes, and a heartbeat has no business importing either.
    """
    t = parse_ts(ts) + timedelta(seconds=seconds)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"
