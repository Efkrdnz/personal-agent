"""The desk says what it is doing, in rows the window can read; and says what the window asks.

The two queues this file drains existed from the first day and nothing ever
drained them; the turn controller had no event sink at all. That is the bug
class this repo is prone to (a missing caller, so nothing raises), so the caller
tests at the bottom matter as much as the behaviour tests above them.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis import __main__ as cli
from jarvis import kill, liveness
from jarvis.audio.graph import AudioEvent, QueuedEventSink
from jarvis.audio.turn import TurnState
from jarvis.bus import Redactor
from jarvis.db import connect, migrate
from jarvis.live.session import LiveEvent, QueuedLiveEvents
from jarvis.voice.desk import DeskPublisher, consume_say_commands, desk_state


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


def kinds(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT kind FROM events ORDER BY seq")]


# ───────────────────────────── the state ─────────────────────────────


class Turn:
    def __init__(self, state: TurnState = TurnState.IDLE, awake: bool = False) -> None:
        self.state, self._awake = state, awake

    def awake(self, at: float | None = None) -> bool:
        return self._awake


@pytest.mark.parametrize(
    ("playing", "turn", "expected"),
    [
        (True, Turn(TurnState.USER_SPEAKING, True), "speaking"),  # the voice you hear wins
        (False, Turn(TurnState.USER_SPEAKING, True), "listening"),
        (False, Turn(TurnState.IDLE, True), "awake"),
        (False, Turn(TurnState.SUSPECT, False), "asleep"),
        (False, Turn(TurnState.IDLE, False), "asleep"),
    ],
)
def test_the_state_is_what_the_orb_should_show(playing: bool, turn: Turn, expected: str) -> None:
    assert desk_state(turn, SimpleNamespace(is_playing=playing)) == expected
    assert expected in liveness.DESK_STATES


# ───────────────────────────── the publisher ─────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_one_step_drains_everything_and_beats(con: sqlite3.Connection) -> None:
    live, audio = QueuedLiveEvents(), QueuedEventSink()
    live(LiveEvent(kind="input_transcript", at=1.0, detail={"text": "hey jarvis"}))
    audio(AudioEvent(kind="wake.awake", at=1.0, detail={}))
    pub = DeskPublisher(
        open_db=lambda: con,
        state=lambda: "awake",
        drains=(lambda c: live.drain(c, "desk"), lambda c: audio.drain(c, "desk")),
    )
    assert pub.step(con) == 2
    assert kinds(con) == ["live.input_transcript", "audio.wake.awake"]
    beat = liveness.read(con, "desk")
    assert beat is not None and beat.state == "awake"


def test_beats_are_throttled_but_a_state_change_beats_at_once(
    con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, state = Clock(), {"now": "asleep"}
    written: list[str] = []
    real_beat = liveness.beat

    def spy(c: sqlite3.Connection, process: str, **kw: object) -> None:
        written.append(str(kw.get("state")))
        real_beat(c, process, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(liveness, "beat", spy)
    pub = DeskPublisher(open_db=lambda: con, state=lambda: state["now"], drains=(), clock=clock)
    pub.step(con)  # first: beats
    clock.t = 0.1
    pub.step(con)  # same state, too soon: no beat
    state["now"] = "listening"
    pub.step(con)  # changed: beats at once
    clock.t = 0.7
    pub.step(con)  # due again
    assert written == ["asleep", "listening", "listening"]


def test_transcripts_are_redacted_on_their_way_into_the_log(con: sqlite3.Connection) -> None:
    live = QueuedLiveEvents()
    live(LiveEvent(kind="input_transcript", at=1.0, detail={"text": "my key is sk-SECRET-123"}))
    red = Redactor.of(["sk-SECRET-123"])
    pub = DeskPublisher(
        open_db=lambda: con,
        state=lambda: "awake",
        drains=(lambda c: live.drain(c, "desk", redactor=red),),
    )
    pub.step(con)
    (payload,) = con.execute(
        "SELECT payload FROM events WHERE kind='live.input_transcript'"
    ).fetchone()
    assert "sk-SECRET-123" not in payload


def test_the_thread_says_offline_when_it_stops(dbpath: Path) -> None:
    live = QueuedLiveEvents()
    pub = DeskPublisher(
        open_db=lambda: connect(dbpath),
        state=lambda: "asleep",
        drains=(lambda c: live.drain(c, "desk"),),
        every_s=0.02,
    )
    pub.start()
    live(LiveEvent(kind="output_transcript", at=1.0, detail={"text": "hello"}))
    deadline = time.monotonic() + 2
    c = connect(dbpath)
    try:
        while time.monotonic() < deadline and "live.output_transcript" not in kinds(c):
            time.sleep(0.02)
        assert "live.output_transcript" in kinds(c)
        pub.stop()
        beat = liveness.read(c, "desk")
        assert beat is not None and beat.state == liveness.OFFLINE
    finally:
        c.close()


def test_a_failing_drain_costs_a_pass_not_the_thread(dbpath: Path) -> None:
    calls = {"n": 0}

    def flaky(c: sqlite3.Connection) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return 0

    pub = DeskPublisher(
        open_db=lambda: connect(dbpath), state=lambda: "awake", drains=(flaky,), every_s=0.01
    )
    pub.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and calls["n"] < 3:
        time.sleep(0.01)
    pub.stop()
    assert pub.failures == 1 and calls["n"] >= 3


# ───────────────────────────── "say this" ─────────────────────────────


def ask(con: sqlite3.Connection, text: str, **kw: object) -> kill.Command:
    args = {"verb": "say", "target_kind": "channel", "target_id": "desk", "args": {"text": text}}
    args.update(kw)
    return kill.issue_command(con, issued_by="window", poke=False, **args)  # type: ignore[arg-type]


def test_say_is_a_verb_and_not_a_kill() -> None:
    assert "say" in kill.VERBS and "say" not in kill.KILL_VERBS


def test_a_say_command_is_spoken_once_and_acknowledged(con: sqlite3.Connection) -> None:
    said: list[str] = []
    cmd = ask(con, "call mum")
    assert consume_say_commands(con, said.append) == 1
    assert consume_say_commands(con, said.append) == 0
    assert said == ["call mum"]
    (ack,) = kill.acks_for(con, cmd.id)
    assert ack.result == "said"
    got = kill.get_command(con, cmd.id)
    assert got is not None and got.state == "done"


def test_a_voice_that_fails_is_reported_in_the_ack(con: sqlite3.Connection) -> None:
    def broken(text: str) -> None:
        raise RuntimeError("no device")

    cmd = ask(con, "hello")
    consume_say_commands(con, broken)
    (ack,) = kill.acks_for(con, cmd.id)
    assert ack.result is not None and ack.result.startswith("failed: RuntimeError: no device")


def test_a_say_for_another_channel_is_left_alone(con: sqlite3.Connection) -> None:
    said: list[str] = []
    ask(con, "for the phone", target_id="phone")
    assert consume_say_commands(con, said.append) == 0 and said == []


def test_two_desks_never_say_it_twice(dbpath: Path) -> None:
    import threading

    c = connect(dbpath)
    try:
        for i in range(5):
            ask(c, f"line {i}")
    finally:
        c.close()
    said: list[str] = []
    lock = threading.Lock()

    def desk() -> None:
        c2 = connect(dbpath)
        try:

            def speak(text: str) -> None:
                with lock:
                    said.append(text)

            consume_say_commands(c2, speak)
        finally:
            c2.close()

    threads = [threading.Thread(target=desk) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(said) == [f"line {i}" for i in range(5)]


# ───────────────────────────── the scheduler and Telegram ─────────────────────────────


def test_the_scheduler_beats_and_says_goodbye(dbpath: Path) -> None:
    from jarvis.schedule.__main__ import main

    assert main(["--db", str(dbpath), "--once"], sleep=lambda s: None) == 0
    c = connect(dbpath)
    try:
        (raw,) = c.execute("SELECT value FROM cursors WHERE name='alive.schedule'").fetchone()
    finally:
        c.close()
    assert json.loads(raw)["state"] == liveness.OFFLINE


def test_the_scheduler_beats_every_tick(con: sqlite3.Connection) -> None:
    from jarvis.schedule.__main__ import run

    run(con, actor="scheduler", claimed_by="t", once=True, sleep=lambda s: None)
    beat = liveness.read(con, "schedule")
    assert beat is not None and beat.state == "running"


# ───────────────────────────── the callers ─────────────────────────────


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


ROOT = Path(cli.__file__)


def test_the_desk_builds_the_publisher_and_wires_both_queues() -> None:
    build = _fn(ROOT, "_build_desk")
    assert {"DeskPublisher", "QueuedLiveEvents", "QueuedEventSink", "desk_redactor"} <= _calls(
        build
    )
    turn = next(
        n
        for n in ast.walk(build)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TurnController"
    )
    assert "on_event" in {k.arg for k in turn.keywords}, "the turn controller had no sink"


def test_the_desk_starts_and_stops_the_publisher_and_consumes_say() -> None:
    desk = _fn(ROOT, "cmd_desk")
    assert {"start", "stop", "consume_say_commands"} <= _calls(desk)


def test_the_scheduler_and_telegram_beat() -> None:
    from jarvis.schedule import __main__ as sched
    from jarvis.telegram import __main__ as tg

    assert "beat" in _calls(_fn(Path(sched.__file__), "run"))
    assert "gone" in _calls(_fn(Path(sched.__file__), "main"))
    assert "beat" in _calls(_fn(Path(tg.__file__), "_tick"))
    assert "gone" in _calls(_fn(Path(tg.__file__), "main"))
