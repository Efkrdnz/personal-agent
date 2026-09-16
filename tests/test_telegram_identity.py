"""Binding: the authentication for this channel, tested as authentication.

A bot username is public, so the interesting tests are all about the attacker
rather than the operator: a stranger messaging the bot, a stranger guessing the
code, two processes racing to spend the same attempt, and a code that outlived
its window.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.db import connect, migrate
from jarvis.ids import now, parse_ts
from jarvis.telegram import identity


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    con = connect(p)
    migrate(con)
    con.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A SECOND process racing the first over one code."""
    c = connect(db_path)
    yield c
    c.close()


def _events(con: sqlite3.Connection, kind: str) -> list[dict]:
    rows = con.execute("SELECT payload FROM events WHERE kind=? ORDER BY seq", (kind,)).fetchall()
    return [json.loads(str(r["payload"])) for r in rows]


def test_nobody_is_authorised_before_a_binding_exists() -> None:
    con = connect(":memory:")
    migrate(con)
    assert identity.bound_chat(con) is None
    assert identity.authorised(con, 12345) is False
    assert identity.authorised(con, None) is False
    con.close()


def test_the_operator_binds_with_a_code_read_off_the_terminal(con: sqlite3.Connection) -> None:
    code = identity.offer_code(con, by="operator")
    outcome = identity.redeem(con, 4242, code, username="efkrdnz")
    assert outcome.ok and outcome.chat_id == 4242
    assert identity.authorised(con, 4242)
    assert identity.authorised(con, 4243) is False


def test_the_plaintext_code_is_never_stored(con: sqlite3.Connection) -> None:
    code = identity.offer_code(con, by="operator")
    stored = con.execute("SELECT value FROM cursors WHERE name=?", (identity.BIND_ROW,)).fetchone()
    assert code not in str(stored["value"])
    # And it is not in the activity log either, which is kept forever.
    dump = con.execute("SELECT group_concat(payload) AS p FROM events").fetchone()["p"] or ""
    assert code not in dump


def test_typing_help_is_tolerated_but_guessing_is_not(con: sqlite3.Connection) -> None:
    code = identity.offer_code(con, by="operator", code="ABCD2345")
    assert identity.redeem(con, 1, "abcd-2345").ok, "case and dashes are a typing aid"
    assert code == "ABCD2345"


def test_three_wrong_guesses_destroy_the_code(con: sqlite3.Connection) -> None:
    identity.offer_code(con, by="operator", code="ABCD2345", attempts=3)
    assert identity.redeem(con, 9, "AAAA1111").attempts_left == 2
    assert identity.redeem(con, 9, "BBBB2222").attempts_left == 1
    last = identity.redeem(con, 9, "CCCC3333")
    assert last.attempts_left == 0
    assert identity.pending_bind(con) is None
    # Even the RIGHT code is worthless now.
    assert identity.redeem(con, 9, "ABCD2345").ok is False
    assert identity.bound_chat(con) is None


def test_a_code_expires_on_the_clock_not_on_use(con: sqlite3.Connection) -> None:
    past = parse_ts(now())
    identity.offer_code(con, by="operator", code="ABCD2345", ttl_s=600)
    late = past.replace(year=past.year + 1)
    stamp = f"{late.strftime('%Y-%m-%dT%H:%M:%S')}.000Z"
    assert identity.pending_bind(con, now_ts=stamp) is None
    outcome = identity.redeem(con, 5, "ABCD2345", now_ts=stamp)
    assert outcome.ok is False
    assert "expired" in outcome.reason


def test_a_code_is_spent_by_use_so_a_second_chat_cannot_reuse_it(
    con: sqlite3.Connection,
) -> None:
    code = identity.offer_code(con, by="operator")
    assert identity.redeem(con, 100, code).ok
    assert identity.redeem(con, 200, code).ok is False
    assert identity.bound_chat(con) == 100


def test_two_processes_racing_one_code_produce_exactly_one_binding(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Two connections is what two daemons look like to SQLite."""
    code = identity.offer_code(con, by="operator")
    first = identity.redeem(con, 111, code)
    second = identity.redeem(other, 222, code)
    assert [first.ok, second.ok] == [True, False]
    assert identity.bound_chat(other) == 111


def test_offering_again_replaces_the_outstanding_offer(con: sqlite3.Connection) -> None:
    identity.offer_code(con, by="operator", code="AAAA1111")
    second = identity.offer_code(con, by="operator", code="BBBB2222")
    assert identity.redeem(con, 1, "AAAA1111").ok is False
    assert identity.redeem(con, 1, second).ok


def test_an_update_from_a_stranger_is_dropped_and_recorded(con: sqlite3.Connection) -> None:
    code = identity.offer_code(con, by="operator")
    identity.redeem(con, 1, code)
    identity.drop_update(con, 99, why="not the bound chat", update_id=7)
    dropped = _events(con, "telegram.dropped")
    assert dropped[-1]["chat_id"] == 99
    assert dropped[-1]["update_id"] == 7


def test_a_failed_guess_is_recorded_with_what_is_left(con: sqlite3.Connection) -> None:
    identity.offer_code(con, by="operator", code="ABCD2345")
    identity.redeem(con, 77, "WRONG")
    failed = _events(con, "telegram.bind_failed")
    assert failed[-1] == {"chat_id": 77, "attempts_left": 2}


def test_unbinding_revokes_everything(con: sqlite3.Connection) -> None:
    code = identity.offer_code(con, by="operator")
    identity.redeem(con, 8, code)
    assert identity.unbind(con, by="operator") is True
    assert identity.authorised(con, 8) is False
    assert identity.unbind(con, by="operator") is False


def test_a_corrupt_binding_row_does_not_lock_the_operator_out(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO cursors (name, value, updated_at) VALUES (?, '{not json', ?)",
        (identity.BIND_ROW, now()),
    )
    code = identity.offer_code(con, by="operator")
    assert identity.redeem(con, 3, code).ok
