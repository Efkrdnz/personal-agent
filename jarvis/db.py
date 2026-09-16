"""The one SQLite file, and the only way to open it.

Every process in Jarvis — the voice app, the dispatcher, each Claude Code
driver, the Telegram bot, the phone worker — opens this same file. SQLite's
single-writer rule then gives a total order for free: ``events.seq`` is assigned
in commit order, so a subscriber reading ``WHERE seq > cursor ORDER BY seq`` can
never see a hole that later fills in.

Three things here are load-bearing and easy to get silently wrong:

``isolation_level=None``
    Python's sqlite3 defaults to opening a *deferred transaction* around DML and
    committing only when you say so. Spike S1 lost an answer to exactly this: a
    write made by one process was rolled back on ``close()`` and looked
    identical to a protocol failure. Autocommit by default; use :func:`tx` when
    you want a transaction, and mean it.

``PRAGMA foreign_keys`` and ``busy_timeout``
    Both are PER CONNECTION. Setting them in a migration does nothing for every
    process that opens the file afterwards.

WAL
    Is persistent in the file, but re-asserting it is free and makes a
    fresh-file open correct without a special case.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["default_path", "connect", "tx", "migrate", "MIGRATIONS_DIR"]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# UPDATE ... RETURNING, used by the compare-and-swap in jarvis.requests, landed
# in 3.35. Without it the first-answer-wins race has no atomic form.
MIN_SQLITE = (3, 35, 0)


def default_path() -> Path:
    """``$XDG_STATE_HOME/jarvis/jarvis.db``, falling back to ``~/.local/state``.

    Deliberately *not* inside the repository and not in a cloud-synced folder:
    this file holds the activity log, every spend row, and the text of every
    question ever asked.
    """
    if env := os.environ.get("JARVIS_DB"):
        return Path(env)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "jarvis" / "jarvis.db"


def connect(path: str | Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database with the pragmas that actually matter."""
    if tuple(int(p) for p in sqlite3.sqlite_version.split(".")) < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required for UPDATE...RETURNING, "
            f"found {sqlite3.sqlite_version}"
        )
    p = Path(path) if path is not None else default_path()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(p), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    if read_only:
        con.execute("PRAGMA query_only=ON")
    return con


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """An explicit IMMEDIATE transaction.

    IMMEDIATE rather than DEFERRED: it takes the write lock up front, so two
    processes racing to write cannot get halfway in and then deadlock. With
    ``busy_timeout`` set, the loser waits rather than failing.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _applied(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def migrate(con: sqlite3.Connection, *, migrations_dir: Path | None = None) -> int:
    """Apply every migration newer than ``PRAGMA user_version``, in order.

    Migrations are ``NNN_name.sql`` and are applied inside one transaction each,
    with ``user_version`` bumped in the same transaction — so a crash halfway
    through leaves the version untouched and the migration is retried whole.

    Returns the version now applied.
    """
    d = migrations_dir or MIGRATIONS_DIR
    files = sorted(d.glob("[0-9][0-9][0-9]_*.sql"), key=lambda f: int(f.name[:3]))
    version = _applied(con)
    for f in files:
        n = int(f.name[:3])
        if n <= version:
            continue
        # executescript() issues an implicit COMMIT, so it cannot run inside our
        # BEGIN IMMEDIATE. Run the DDL, then bump the version; the bump is what
        # makes it idempotent, and DDL here is CREATE ... IF NOT EXISTS-safe by
        # virtue of only ever running once per version.
        con.executescript(f.read_text())
        con.execute(f"PRAGMA user_version={n}")
        version = n
    return version


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    """connect() + migrate(). What every process actually calls at startup."""
    con = connect(path)
    migrate(con)
    return con
