"""Long polling, and the cursor that makes a restart neither replay nor skip.

The cursor is the only durable state this loop has, so every test here is really
about a process that died: mid-batch, between handling and committing, or
overlapping its own successor.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.db import connect, migrate
from jarvis.telegram import bot
from jarvis.telegram.transport import FakeTransport, TelegramError


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
def restarted(db_path: Path) -> Iterator[sqlite3.Connection]:
    """The same bot, started again after the first process died."""
    c = connect(db_path)
    yield c
    c.close()


def _update(n: int, text: str = "hi") -> dict[str, Any]:
    return {"update_id": n, "message": {"message_id": n, "chat": {"id": 1}, "text": text}}


def test_a_fresh_machine_asks_from_the_beginning(con: sqlite3.Connection) -> None:
    t = FakeTransport(updates=[[_update(7)]])
    bot.poll_once(con, t)
    assert t.last("getUpdates").params["offset"] == 1  # type: ignore[union-attr]


def test_the_cursor_advances_only_after_the_update_was_handled(con: sqlite3.Connection) -> None:
    t = FakeTransport(updates=[[_update(10), _update(11)]])
    seen: list[int] = []

    def handle(c: sqlite3.Connection, update: dict[str, Any]) -> None:
        # The cursor still points BEHIND this update while it is being handled,
        # which is what makes a crash here a replay rather than a loss.
        assert bot.read_offset(c) < int(update["update_id"])
        seen.append(int(update["update_id"]))

    assert bot.run(con, t, handle) == 2
    assert seen == [10, 11]
    assert bot.read_offset(con) == 11


def test_a_restart_neither_replays_nor_skips(
    con: sqlite3.Connection, restarted: sqlite3.Connection
) -> None:
    first = FakeTransport(updates=[[_update(20), _update(21)]])
    seen: list[int] = []
    bot.run(con, first, lambda c, u: seen.append(int(u["update_id"])))

    # New process, new connection, same file. The server still has 21 and 22.
    second = FakeTransport(updates=[[_update(21), _update(22)]])
    bot.run(restarted, second, lambda c, u: seen.append(int(u["update_id"])))

    assert seen == [20, 21, 22], "21 was handled once and 22 was not missed"
    assert second.sent("getUpdates")[0].params["offset"] == 22


def test_the_cursor_only_ever_moves_forward(con: sqlite3.Connection) -> None:
    """Two overlapping loops committing out of order must not re-deliver."""
    bot.commit_offset(con, 50)
    assert bot.commit_offset(con, 40) == 50
    assert bot.commit_offset(con, 51) == 51


def test_a_poison_update_is_logged_loudly_and_does_not_wedge_the_loop(
    con: sqlite3.Connection,
) -> None:
    """A loop stuck retrying one update is a channel that has silently stopped."""
    t = FakeTransport(updates=[[_update(30), _update(31)]])
    seen: list[int] = []

    def handle(c: sqlite3.Connection, update: dict[str, Any]) -> None:
        if int(update["update_id"]) == 30:
            raise RuntimeError("that update makes no sense")
        seen.append(int(update["update_id"]))

    assert bot.run(con, t, handle) == 2
    assert seen == [31]
    assert bot.read_offset(con) == 31
    failures = con.execute(
        "SELECT payload FROM events WHERE kind='telegram.update_failed'"
    ).fetchall()
    assert json.loads(str(failures[0]["payload"]))["update_id"] == 30


def test_a_failed_poll_confirms_nothing(con: sqlite3.Connection) -> None:
    slept: list[float] = []
    t = FakeTransport(errors={"getUpdates": [TelegramError("getUpdates", 502, "bad gateway")]})
    assert bot.run(con, t, lambda c, u: None, sleep=slept.append) == 0
    assert bot.read_offset(con) == 0
    kinds = [r["kind"] for r in con.execute("SELECT kind FROM events").fetchall()]
    assert "telegram.poll_failed" in kinds


def test_one_bad_gateway_does_not_end_the_channel(con: sqlite3.Connection) -> None:
    """A 502 on a connection held open for twenty seconds is a normal event."""
    slept: list[float] = []
    t = FakeTransport(
        updates=[[_update(40)]],
        errors={"getUpdates": [TelegramError("getUpdates", 502, "bad gateway")]},
    )
    seen: list[int] = []
    assert bot.run(con, t, lambda c, u: seen.append(int(u["update_id"])), sleep=slept.append) == 1
    assert seen == [40]
    assert slept == [2.0], "it backed off once rather than hammering or dying"


def test_it_gives_up_after_enough_failures_in_a_row(con: sqlite3.Connection) -> None:
    """A revoked token would otherwise retry forever and look alive."""
    slept: list[float] = []
    t = FakeTransport(errors={"getUpdates": [TelegramError("getUpdates", 401, "unauthorized")] * 9})
    assert bot.run(con, t, lambda c, u: None, sleep=slept.append, failure_limit=3) == 0
    assert len(t.sent("getUpdates")) == 3
    assert len(slept) == 2


def test_the_tick_runs_even_when_nothing_arrives(con: sqlite3.Connection) -> None:
    """Due deliveries must not wait for somebody to type."""
    ticks: list[int] = []
    bot.run(con, FakeTransport(updates=[]), lambda c, u: None, on_tick=lambda c: ticks.append(1))
    assert ticks == [1]


def test_only_the_update_kinds_this_channel_understands_are_requested(
    con: sqlite3.Connection,
) -> None:
    t = FakeTransport(updates=[[_update(1)]])
    bot.poll_once(con, t)
    assert t.last("getUpdates").params["allowed_updates"] == [  # type: ignore[union-attr]
        "message",
        "callback_query",
    ]


def test_the_socket_timeout_outlives_the_long_poll(con: sqlite3.Connection) -> None:
    """Otherwise every call times out exactly when the API is behaving."""
    calls: list[float | None] = []

    class _Timed(FakeTransport):
        def call(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
            calls.append(timeout)
            return super().call(method, params, timeout=timeout)

    t = _Timed(updates=[[_update(1)]])
    bot.poll_once(con, t, timeout_s=20)
    assert calls[0] is not None and calls[0] > 20


def test_a_failing_tick_does_not_take_the_whole_channel_down(tmp_path: Path) -> None:
    """The tick presents due deliveries, and a presentation can fail on its own.

    Letting that out of the loop makes ONE undeliverable question unreachable
    everything else too — the exact failure the poison-update handler avoids.
    """
    con = connect(tmp_path / "tick.db")
    migrate(con)
    seen: list[int] = []

    def exploding_tick(c: sqlite3.Connection) -> None:
        seen.append(1)
        raise RuntimeError("sendMessage said 500")

    t = FakeTransport(updates=[[{"update_id": 7, "message": {"text": "hi"}}]])
    handled = bot.run(con, t, lambda c, u: None, on_tick=exploding_tick, sleep=lambda _: None)

    assert handled == 1, "the update was still handled"
    assert len(seen) >= 1
    logged = con.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='telegram.tick_failed'"
    ).fetchone()["n"]
    assert logged >= 1, "and the failure is in the log rather than swallowed"
    con.close()
