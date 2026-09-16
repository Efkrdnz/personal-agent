"""The convenience commands, and the one that is not convenient at all.

``/kill`` is the test that matters here: the runner is a DIFFERENT OS PROCESS
with a different connection, and the only thing that reaches it is a number in a
SQLite file. Two connections is what that looks like from SQLite's point of view.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs, kill, ledger, presence
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.telegram import commands

CHAT = 4242


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
def runner(db_path: Path) -> Iterator[sqlite3.Connection]:
    """The Claude Code driver: another process, another connection."""
    c = connect(db_path)
    yield c
    c.close()


def _say(con: sqlite3.Connection, text: str) -> str | None:
    reply = commands.handle_command(con, text, chat_id=CHAT)
    return None if reply is None else reply.text


# ───────────────────────────── dispatch ─────────────────────────────


def test_something_that_is_not_a_command_is_left_for_the_answer_path(
    con: sqlite3.Connection,
) -> None:
    """Swallowing every message would make 'none of these' unreachable."""
    assert _say(con, "DuckDB, actually") is None
    assert _say(con, "") is None


def test_help_lists_what_there_is(con: sqlite3.Connection) -> None:
    text = _say(con, "/start") or ""
    for command in ("/status", "/log today", "/spend", "/kill"):
        assert command in text


# ───────────────────────────── /status ─────────────────────────────


def test_status_names_the_running_jobs(con: sqlite3.Connection) -> None:
    job = jobs.create_job(con, kind="claude_code", title="the todo app build", created_by="desk")
    jobs.set_state(con, job.id, "starting")
    jobs.set_state(con, job.id, "running")
    text = _say(con, "/status") or ""
    assert "the todo app build" in text


def test_status_counts_the_questions_waiting_for_you(con: sqlite3.Connection) -> None:
    from jarvis import requests as rq

    rq.create_request(
        con,
        kind="free_text",
        short_label="a question",
        presentation=rq.make_presentation(intro="Well?", options=["Yes"]),
        payload={},
        actor="test",
    )
    assert "1 open question" in (_say(con, "/status") or "")


def test_status_does_not_advance_the_briefing_cursor(con: sqlite3.Connection) -> None:
    """Asking on a phone must not make the morning briefing skip a section."""
    from jarvis.reconcile import briefing_cursor

    before = briefing_cursor(con)
    _say(con, "/status")
    assert briefing_cursor(con) == before


# ───────────────────────────── /spend ─────────────────────────────


def test_spend_carries_the_unpriced_meters_because_zero_is_not_free(
    con: sqlite3.Connection,
) -> None:
    ledger.record(con, "claude_code", "rate_window_pct", 41.0)
    text = _say(con, "/spend") or ""
    assert "rate_window_pct" in text
    assert "not convertible to dollars" in text
    assert "not the same as free" in text.lower() or "does not include" in text


def test_spend_shows_dollars_when_there_are_any(con: sqlite3.Connection) -> None:
    ledger.record(con, "gemini", "gemini_sec", 120.0, usd_equiv=0.36, estimated=True)
    text = _say(con, "/spend") or ""
    assert "0.36" in text


# ───────────────────────────── /log ─────────────────────────────


def test_log_today_shows_the_activity_log(con: sqlite3.Connection) -> None:
    from jarvis.bus import publish

    publish(con, "job.created", "desk", {"title": "the todo app build"})
    text = _say(con, "/log today") or ""
    assert "job.created" in text
    assert "desk" in text


def test_log_today_starts_at_LOCAL_midnight(con: sqlite3.Connection, monkeypatch) -> None:
    """The database is UTC; 'today' is not. Istanbul is three hours ahead."""
    monkeypatch.setenv("JARVIS_TZ", "Europe/Istanbul")
    con.execute(
        """INSERT INTO events (seq, id, ts, kind, actor, idem_key, payload, prev_hash, hash)
           VALUES (900, 'ev_yesterday', '2026-09-15T20:00:00.000Z', 'job.created',
                   'desk', 'k1', '{}', 'x', 'y')"""
    )
    con.execute(
        """INSERT INTO events (seq, id, ts, kind, actor, idem_key, payload, prev_hash, hash)
           VALUES (901, 'ev_today', '2026-09-15T21:30:00.000Z', 'job.finished',
                   'desk', 'k2', '{}', 'x', 'y')"""
    )
    # 21:30Z on the 15th is 00:30 on the 16th in Istanbul: today.
    text = commands.log_today(con, now_ts="2026-09-16T09:00:00.000Z")
    assert "job.finished" in text
    assert "job.created" not in text


# ───────────────────────────── /kill ─────────────────────────────


def test_kill_from_a_phone_stops_a_runner_in_another_process(
    con: sqlite3.Connection, runner: sqlite3.Connection
) -> None:
    job = jobs.create_job(con, kind="claude_code", title="the todo app build", created_by="desk")
    jobs.set_state(con, job.id, "starting")
    jobs.set_state(con, job.id, "running")
    epoch = kill.stamp_job_epoch(runner, job.id)

    text = _say(con, "/kill") or ""
    assert "the todo app build" in text

    # Nothing was signalled. The runner learns by re-reading the file.
    assert kill.epoch_is_current(runner, epoch) is False
    with pytest.raises(kill.KillEpochAdvanced):
        kill.assert_epoch(runner, epoch)


def test_kill_with_nothing_running_says_so(con: sqlite3.Connection) -> None:
    assert "Nothing was running" in (_say(con, "/kill") or "")


# ───────────────────────────── presence ─────────────────────────────


def test_going_out_and_coming_back(con: sqlite3.Connection) -> None:
    assert "Noted" in (_say(con, "I'm going out") or "")
    assert presence.read_override(con).mode == "away"
    assert "telegram" in presence.read_presence(con).reachable

    assert "cleared" in (_say(con, "I'm back") or "").lower()
    assert presence.read_override(con).mode is None


def test_a_phone_keyboards_curly_apostrophe_works_too(con: sqlite3.Connection) -> None:
    """U+2019 from a phone and U+0027 from a laptop are the same sentence."""
    assert _say(con, "I’m going out") is not None
    assert presence.read_override(con).mode == "away"


def test_dont_call_me_and_desk_only(con: sqlite3.Connection) -> None:
    _say(con, "don't call me")
    assert presence.read_override(con).mode == "dnd"
    _say(con, "desk only")
    assert presence.read_override(con).mode == "desk_only"


def test_an_override_is_always_bounded(con: sqlite3.Connection) -> None:
    """Nobody should be unreachable until next Friday because of one sentence."""
    _say(con, "I'm going out")
    override = presence.read_override(con)
    assert override.until is not None and override.until > now()
