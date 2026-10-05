"""Heartbeats as rows: what the window believes about processes it cannot ask.

The failures worth a test: a ``since`` that resets on every beat (the orb would
say "listening for 0 s" forever), a ``since`` that survives a state change or a
crash-and-restart, a dead process shown as alive, a goodbye shown as a crash,
and two connections beating one name at once.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import liveness
from jarvis.db import connect, migrate
from jarvis.ids import now

T0 = "2026-10-05T12:00:00.000Z"
T1 = "2026-10-05T12:00:01.000Z"
T2 = "2026-10-05T12:00:02.500Z"
T3 = "2026-10-05T12:00:03.000Z"


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(dbpath: Path) -> Iterator[sqlite3.Connection]:
    c = connect(dbpath)
    yield c
    c.close()


def _row(con: sqlite3.Connection, process: str) -> dict:
    raw = con.execute("SELECT value FROM cursors WHERE name=?", (f"alive.{process}",)).fetchone()
    return json.loads(raw[0])


def test_round_trip(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="asleep", detail={"mic": "ok"}, now_ts=T0)
    b = liveness.read(con, "desk", now_ts=T1)
    assert b is not None
    assert b.process == "desk"
    assert b.pid == os.getpid()
    assert b.state == "asleep"
    assert b.detail == {"mic": "ok"}
    assert b.since == T0
    assert b.at == T0
    assert b.age_s == pytest.approx(1.0)


def test_the_row_lives_in_cursors_under_alive_dot_name(con: sqlite3.Connection) -> None:
    liveness.beat(con, "schedule", state="running", now_ts=T0)
    body = _row(con, "schedule")
    assert body == {"pid": os.getpid(), "state": "running", "detail": {}, "since": T0}
    assert liveness.key("schedule") == "alive.schedule"


def test_since_is_kept_while_the_state_is_unchanged(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="listening", now_ts=T0)
    liveness.beat(con, "desk", state="listening", now_ts=T1)
    liveness.beat(con, "desk", state="listening", now_ts=T2)
    b = liveness.read(con, "desk", now_ts=T2)
    assert b is not None
    assert b.since == T0
    assert b.at == T2


def test_since_resets_when_the_state_changes(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="listening", now_ts=T0)
    liveness.beat(con, "desk", state="speaking", now_ts=T1)
    b = liveness.read(con, "desk", now_ts=T2)
    assert b is not None
    assert (b.state, b.since) == ("speaking", T1)
    liveness.beat(con, "desk", state="listening", now_ts=T2)
    b = liveness.read(con, "desk", now_ts=T3)
    assert b is not None
    assert (b.state, b.since) == ("listening", T2)


def test_since_resets_after_a_gap_long_enough_to_be_a_crash(con: sqlite3.Connection) -> None:
    # Same state, but nobody beat for a minute: "awake for 61 s" would include
    # a minute when no desk was running at all.
    liveness.beat(con, "desk", state="awake", now_ts=T0)
    later = "2026-10-05T12:01:00.000Z"
    liveness.beat(con, "desk", state="awake", now_ts=later)
    b = liveness.read(con, "desk", now_ts=later)
    assert b is not None
    assert b.since == later


def test_since_resets_when_a_different_pid_takes_over_the_name(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="awake", now_ts=T0)
    # A restarted desk: same name, same state, another process.
    body = _row(con, "desk")
    body["pid"] = body["pid"] + 1
    con.execute("UPDATE cursors SET value=? WHERE name='alive.desk'", (json.dumps(body),))
    liveness.beat(con, "desk", state="awake", now_ts=T1)
    b = liveness.read(con, "desk", now_ts=T1)
    assert b is not None
    assert b.since == T1


def test_a_stale_beat_reads_as_none(con: sqlite3.Connection) -> None:
    liveness.beat(con, "telegram", state="polling", now_ts=T0)
    assert liveness.read(con, "telegram", now_ts="2026-10-05T12:00:09.000Z") is not None
    assert liveness.read(con, "telegram", now_ts="2026-10-05T12:00:11.000Z") is None
    assert liveness.read(con, "telegram", within_s=60, now_ts="2026-10-05T12:00:30.000Z")


def test_no_row_reads_as_none(con: sqlite3.Connection) -> None:
    assert liveness.read(con, "desk") is None
    assert liveness.read_all(con) == {p: None for p in liveness.PROCESSES}


def test_a_garbage_row_reads_as_none_never_raises(con: sqlite3.Connection) -> None:
    for junk in ("not json", "[1,2]", '{"pid": "x"}', '{"pid": 1, "state": 5}'):
        con.execute(
            "INSERT OR REPLACE INTO cursors(name, value, updated_at) VALUES ('alive.desk', ?, ?)",
            (junk, T0),
        )
        assert liveness.read(con, "desk", now_ts=T1) is None
    # And a beat over the garbage repairs it rather than tripping on it.
    liveness.beat(con, "desk", state="asleep", now_ts=T1)
    b = liveness.read(con, "desk", now_ts=T1)
    assert b is not None
    assert (b.state, b.since) == ("asleep", T1)


def test_a_writer_clock_slightly_ahead_is_fresh_not_negative(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="asleep", now_ts=T2)
    b = liveness.read(con, "desk", now_ts=T1)
    assert b is not None
    assert b.age_s == 0.0


def test_gone_writes_offline_at_once(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="speaking", now_ts=T0)
    liveness.gone(con, "desk", now_ts=T1)
    b = liveness.read(con, "desk", now_ts=T1)
    assert b is not None
    assert b.state == liveness.OFFLINE == "offline"
    assert b.since == T1


def test_desk_state_must_be_one_the_window_can_draw(con: sqlite3.Connection) -> None:
    for state in liveness.DESK_STATES:
        liveness.beat(con, "desk", state=state, now_ts=T0)
    with pytest.raises(ValueError, match="desk state"):
        liveness.beat(con, "desk", state="thinking", now_ts=T0)
    with pytest.raises(ValueError, match="desk state"):
        liveness.beat(con, "desk", now_ts=T0)
    # Other processes say what they like.
    liveness.beat(con, "schedule", state="anything", now_ts=T0)
    liveness.beat(con, "telegram", now_ts=T0)


def test_an_unknown_process_name_is_refused(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown process"):
        liveness.beat(con, "scheduler", state="running")
    with pytest.raises(ValueError, match="unknown process"):
        liveness.read(con, "scheduler")


def test_detail_must_be_a_dict(con: sqlite3.Connection) -> None:
    with pytest.raises(TypeError):
        liveness.beat(con, "schedule", state="running", detail=["x"])  # type: ignore[arg-type]


def test_read_all_reads_every_process_in_one_go(con: sqlite3.Connection) -> None:
    liveness.beat(con, "desk", state="awake", now_ts=T0)
    liveness.beat(con, "schedule", state="running", now_ts=T0)
    beats = liveness.read_all(con, now_ts=T1)
    assert set(beats) == set(liveness.PROCESSES)
    assert beats["desk"] is not None and beats["desk"].state == "awake"
    assert beats["schedule"] is not None and beats["schedule"].state == "running"
    assert beats["telegram"] is None and beats["window"] is None


def test_two_connections_beating_from_two_threads(dbpath: Path) -> None:
    """Rule 3: a race gets a test with two real connections."""
    errors: list[BaseException] = []
    start = threading.Barrier(2)
    first = now()
    c0 = connect(dbpath)
    liveness.beat(c0, "desk", state="listening", now_ts=first)
    c0.close()

    def run(process: str, states: tuple[str, ...]) -> None:
        c = connect(dbpath)
        try:
            start.wait(timeout=5)
            for i in range(60):
                liveness.beat(c, process, state=states[i % len(states)])
                liveness.beat(c, "desk", state="listening")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)
        finally:
            c.close()

    threads = [
        threading.Thread(target=run, args=("desk", ("listening",))),
        threading.Thread(target=run, args=("schedule", ("running", "idle"))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    c = connect(dbpath)
    try:
        desk = liveness.read(c, "desk")
        sched = liveness.read(c, "schedule")
        n = c.execute("SELECT COUNT(*) FROM cursors WHERE name LIKE 'alive.%'").fetchone()[0]
    finally:
        c.close()
    assert desk is not None and desk.state == "listening"
    assert sched is not None and sched.state in ("running", "idle")
    # Both threads beat "desk" with one state throughout, so its clock never
    # restarted even though two connections were writing it at once.
    assert desk.since == first
    assert n == 2


def test_the_module_is_spine_and_imports_without_site_packages() -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-S", "-c", "import jarvis.liveness"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr


# ───────────────────────────── slow beaters ─────────────────────────────


def test_a_process_that_beats_slowly_is_not_called_dead_between_beats() -> None:
    """The scheduler ticks every 15 s and Telegram long-polls 20: the 10 s rule flickered."""
    from jarvis.db import connect, migrate
    from jarvis.liveness import beat, read

    con = connect(":memory:")
    migrate(con)
    beat(
        con, "schedule", state="running", detail={"every_s": 15}, now_ts="2026-10-05T10:00:00.000Z"
    )
    assert read(con, "schedule", now_ts="2026-10-05T10:00:14.000Z") is not None
    assert read(con, "schedule", now_ts="2026-10-05T10:00:44.000Z") is not None
    assert read(con, "schedule", now_ts="2026-10-05T10:00:46.000Z") is None  # three missed: gone
    # Its `since` survives a beat 15 s later, because 15 s is not a gap for it.
    beat(
        con, "schedule", state="running", detail={"every_s": 15}, now_ts="2026-10-05T10:00:15.000Z"
    )
    b = read(con, "schedule", now_ts="2026-10-05T10:00:16.000Z")
    assert b is not None and b.since == "2026-10-05T10:00:00.000Z"


def test_a_nonsense_interval_falls_back_to_the_default() -> None:
    from jarvis.db import connect, migrate
    from jarvis.liveness import beat, read

    con = connect(":memory:")
    migrate(con)
    for bad in (True, -5, "15", None):
        beat(
            con,
            "telegram",
            state="running",
            detail={"every_s": bad},
            now_ts="2026-10-05T10:00:00.000Z",
        )
        assert read(con, "telegram", now_ts="2026-10-05T10:00:11.000Z") is None
