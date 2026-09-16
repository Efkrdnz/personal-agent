"""The session, driven by a scripted server: reconnects, handles, tools, drops.

There is no API key on this machine and there never will be one in CI, so every
test here runs against :mod:`jarvis.live.fake` — except the ones that check the
translation of the SDK's own message objects, which are built by hand from the
real ``google.genai`` types and prove this layer reads the wire format the SDK
actually produces rather than the one it was remembered to produce.

Nothing here sleeps for real time. The session's sleeper is injected and the
tests hand it one that returns immediately, so a 4-second backoff costs a
microsecond and the assertion is on the number it was ASKED to sleep.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import bus
from jarvis.db import connect, migrate
from jarvis.live import fake
from jarvis.live.fake import (
    Drop,
    FrameSource,
    Hangup,
    Pause,
    ScriptedConnector,
)
from jarvis.live.profiles import AGENT_CALL, DESK, SessionProfile
from jarvis.live.session import (
    ActivityMark,
    GenaiConnector,
    LiveDown,
    LiveEvent,
    LiveSession,
    LiveUnavailable,
    NotConnected,
    QueuedLiveEvents,
    QueuedUplink,
    SimpleToolRegistry,
    ToolCall,
    ToolResult,
    Usage,
    record_session_time,
    record_usage,
    server_event,
)
from jarvis.voice.router import FidelityViolation, Utterance

# ───────────────────────────── harness ─────────────────────────────


class Sleeper:
    """An injected sleep that records what it was asked for and returns at once."""

    def __init__(self) -> None:
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        await asyncio.sleep(0)


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


async def settle(times: int = 60) -> None:
    """Let every runnable task run. Scheduling, not waiting."""
    for _ in range(times):
        await asyncio.sleep(0)


async def until(predicate: Any, tries: int = 2000) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def make_session(
    connector: ScriptedConnector,
    *,
    profile: SessionProfile = DESK,
    source: Any = None,
    sink: Any = None,
    tools: Any = None,
    events: Any = None,
    clock: Any = None,
    sleeper: Any = None,
    **kwargs: Any,
) -> LiveSession:
    return LiveSession(
        profile,
        source,
        sink,
        connector=connector,
        tools=tools,
        on_event=events,
        clock=clock or Clock(),
        sleep=sleeper or Sleeper(),
        idle_s=0.0,
        **kwargs,
    )


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "jarvis.db"
    c = connect(path)
    migrate(c)
    yield c
    c.close()


# ───────────────────── the handle, which is the whole point ─────────────────────


async def test_go_away_reconnects_and_replays_the_handle() -> None:
    connector = ScriptedConnector(
        scripts=[
            [fake.handle("h-1"), fake.audio(b"aaa"), fake.go_away(5.0), fake.turn_complete()],
            [fake.audio(b"bbb")],
        ]
    )
    sink = fake.BytesSink()
    session = make_session(connector, sink=sink)
    task = asyncio.create_task(session.run())

    await until(lambda: len(connector.opened) == 2)
    assert connector.handles == [None, "h-1"]
    assert session.go_aways == 1
    assert session.resumes == 1

    await session.close()
    await task
    assert sink.audio() == b"aaabbb"


async def test_a_mid_turn_disconnect_does_not_lose_the_handle() -> None:
    connector = ScriptedConnector(
        scripts=[
            [fake.handle("h-7"), fake.audio(b"half a sentence"), Drop()],
            [fake.audio(b"...and the rest")],
        ]
    )
    sleeper = Sleeper()
    session = make_session(connector, sink=fake.BytesSink(), sleeper=sleeper)
    task = asyncio.create_task(session.run())

    await until(lambda: len(connector.opened) == 2)
    assert connector.handles == [None, "h-7"]
    assert session.resume_handle == "h-7"
    # A drop is not a connect failure, but it still backs off a little.
    assert sleeper.asked == [0.25]

    await session.close()
    await task


async def test_a_handle_that_is_not_resumable_is_not_kept() -> None:
    connector = ScriptedConnector(scripts=[[fake.handle("h-x", resumable=False), Hangup()], []])
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: len(connector.opened) == 2)
    assert connector.handles == [None, None]
    await session.close()
    await task


async def test_close_with_checkpoint_keeps_the_handle_and_without_it_forgets() -> None:
    connector = ScriptedConnector(scripts=[[fake.handle("h-9")]], repeat_last=True)
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: session.resume_handle == "h-9")
    await session.close(checkpoint=True)
    await task
    assert session.resume_handle == "h-9"

    await session.close(checkpoint=False)
    assert session.resume_handle is None


async def test_changing_voice_reconnects_without_losing_the_conversation() -> None:
    # Reference problem 3, made impossible: voice is set at connect time, so a
    # change means a reconnect — but the handle goes with it.
    connector = ScriptedConnector(
        scripts=[[fake.handle("h-voice"), fake.audio(b"x"), Pause()], [fake.audio(b"y")]]
    )
    session = make_session(connector, sink=fake.BytesSink())
    task = asyncio.create_task(session.run())
    await until(lambda: session.resume_handle == "h-voice")

    await session.change_voice("Puck")
    pause = next(s for s in connector.opened[0]._script if isinstance(s, Pause))
    pause.event.set()

    await until(lambda: len(connector.opened) == 2)
    assert connector.handles == [None, "h-voice"]
    assert connector.voices == ["Zephyr", "Puck"]
    await session.close()
    await task


# ───────────────────────────── the uplink ─────────────────────────────


async def test_audio_goes_up_tagged_with_its_rate_and_marks_keep_their_order() -> None:
    source = FrameSource(
        [ActivityMark.START, b"\x01\x02" * 160, b"\x03\x04" * 160, ActivityMark.END]
    )
    connector = ScriptedConnector(scripts=[[Pause()]])
    session = make_session(connector, source=source)
    task = asyncio.create_task(session.run())

    await until(lambda: connector.opened and len(connector.last.marks) == 2)
    transport = connector.last
    assert transport.marks == [ActivityMark.START, ActivityMark.END]
    assert transport.sent_mime == ["audio/pcm;rate=16000"] * 2
    assert transport.audio_bytes == 640
    assert session.frames_sent == 2

    await session.close()
    await task


async def test_a_gated_uplink_reads_and_discards_rather_than_muting() -> None:
    # Rule 1: the microphone is never gated off. The gate decides only what
    # Gemini is TOLD about, which is what keeps the spotter alive while the
    # reader voice is speaking.
    source = FrameSource([b"\x01\x02" * 160])
    connector = ScriptedConnector(scripts=[[Pause()]])
    session = make_session(connector, source=source)
    session.gate_uplink("reading options")
    task = asyncio.create_task(session.run())

    await until(lambda: session.frames_gated == 1)
    assert connector.last.sent_audio == []
    reads_while_gated = source.reads
    assert reads_while_gated > 0

    source.feed(b"\x05\x06" * 160)
    session.ungate_uplink()
    await until(lambda: connector.last.sent_audio != [])
    assert session.frames_sent == 1

    await session.close()
    await task


async def test_the_queued_uplink_is_the_audio_threads_side_of_the_same_stream() -> None:
    uplink = QueuedUplink(limit=3)
    uplink.activity_start()
    uplink.send(b"\x00" * 320)
    uplink.activity_end()
    assert len(uplink) == 3
    uplink.send(b"\x01" * 320)  # over the limit: the OLDEST goes
    assert uplink.dropped == 1
    assert uplink.read() == b"\x00" * 320
    assert uplink.read() is ActivityMark.END
    assert uplink.read() == b"\x01" * 320
    assert uplink.read() is None


# ───────────────────────────── the local drop ─────────────────────────────


async def test_barge_in_drops_downlink_audio_locally_until_the_turn_ends() -> None:
    # There is no interrupt() on the Live session, so this is the only lever
    # that always works.
    pause = Pause()
    connector = ScriptedConnector(
        scripts=[
            [
                fake.audio(b"before"),
                pause,
                fake.audio(b"after"),
                fake.turn_complete(),
                fake.audio(b"next turn"),
            ]
        ]
    )
    sink = fake.BytesSink()
    session = make_session(connector, sink=sink)
    task = asyncio.create_task(session.run())

    await until(lambda: sink.audio() == b"before")
    await session.drop_output("barge_in")
    pause.event.set()

    await until(lambda: sink.audio() == b"beforenext turn")
    assert session.audio_bytes_dropped == len(b"after")
    assert session.dropping is False
    await session.close()
    await task


async def test_a_drop_with_no_boundary_expires_instead_of_muting_forever() -> None:
    clock = Clock()
    pause = Pause()
    connector = ScriptedConnector(scripts=[[fake.audio(b"one"), pause, fake.audio(b"two")]])
    sink = fake.BytesSink()
    session = make_session(connector, sink=sink, clock=clock, drop_ttl_s=2.0)
    task = asyncio.create_task(session.run())

    await until(lambda: sink.audio() == b"one")
    await session.drop_output()
    clock.advance(2.5)  # the turn boundary never came
    pause.event.set()
    await until(lambda: sink.audio() == b"onetwo")

    await session.close()
    await task


async def test_the_synthetic_barge_in_is_only_sent_when_a_profile_arms_it() -> None:
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)
    await session.drop_output()
    assert connector.last.marks == []
    await session.close()
    await task

    armed = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    session = make_session(armed, profile=SessionProfile(name="desk", synthetic_barge_in=True))
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)
    await session.drop_output()
    assert armed.last.marks == [ActivityMark.START]
    await session.close()
    await task


async def test_a_server_interrupt_clears_the_local_drop() -> None:
    pause = Pause()
    connector = ScriptedConnector(
        scripts=[[fake.audio(b"a"), pause, fake.ServerEvent(interrupted=True), fake.audio(b"b")]]
    )
    sink = fake.BytesSink()
    session = make_session(connector, sink=sink)
    task = asyncio.create_task(session.run())
    await until(lambda: sink.audio() == b"a")
    await session.drop_output()
    pause.event.set()
    await until(lambda: sink.audio() == b"ab")
    await session.close()
    await task


# ───────────────────────────── tools ─────────────────────────────


def registry() -> tuple[SimpleToolRegistry, list[ToolCall]]:
    seen: list[ToolCall] = []

    async def answer_question(qid: str = "", picks: list[int] | None = None) -> dict[str, Any]:
        seen.append(ToolCall(id="", name="answer_question", args={"qid": qid, "picks": picks}))
        return {"ok": True, "qid": qid, "picks": picks or []}

    reg = SimpleToolRegistry()
    reg.add("answer_question", answer_question, description="commit an answer by index")
    return reg, seen


async def test_a_tool_call_round_trips_through_the_registry() -> None:
    reg, seen = registry()
    connector = ScriptedConnector(
        scripts=[[fake.tool_call("tc-1", "answer_question", qid="q1", picks=[2])]],
        repeat_last=True,
    )
    session = make_session(connector, tools=reg)
    task = asyncio.create_task(session.run())

    await until(lambda: bool(connector.opened) and connector.last.tool_responses != [])
    response = connector.last.tool_responses[0]
    assert response.id == "tc-1"
    assert response.name == "answer_question"
    assert response.response == {"ok": True, "qid": "q1", "picks": [2]}
    assert seen[0].args == {"qid": "q1", "picks": [2]}
    # SILENT by default: a tool result is context, not a reason to start talking.
    assert response.scheduling == "SILENT"
    assert connector.declarations[0] == [
        {
            "name": "answer_question",
            "description": "commit an answer by index",
            "parameters": {"type": "OBJECT", "properties": {}},
        }
    ]

    await session.close()
    await task


async def test_a_long_tool_does_not_make_the_session_deaf() -> None:
    # Reference problem 2: tools awaited inline in the receive loop mean a long
    # Claude Code job makes the whole assistant deaf and mute.
    gate = asyncio.Event()

    async def slow_build(**_: Any) -> dict[str, Any]:
        await gate.wait()
        return {"job_id": "j-1"}

    reg = SimpleToolRegistry()
    reg.add("code_build", slow_build)
    connector = ScriptedConnector(
        scripts=[
            [
                fake.tool_call("tc-2", "code_build"),
                fake.audio(b"still talking"),
                fake.turn_complete(),
            ]
        ],
        repeat_last=True,
    )
    sink = fake.BytesSink()
    session = make_session(connector, sink=sink, tools=reg)
    task = asyncio.create_task(session.run())

    await until(lambda: sink.audio() == b"still talking")
    assert connector.last.tool_responses == []  # still running
    gate.set()
    await until(lambda: bool(connector.opened) and connector.last.tool_responses != [])
    assert connector.last.tool_responses[0].response == {"job_id": "j-1"}

    await session.close()
    await task


async def test_a_tool_the_profile_does_not_declare_is_refused_not_run() -> None:
    ran: list[str] = []

    reg = SimpleToolRegistry()
    reg.add("place_order", lambda **_: ran.append("place_order") or {"ok": True})
    connector = ScriptedConnector(
        scripts=[[fake.tool_call("tc-3", "place_order")]], repeat_last=True
    )
    session = make_session(connector, profile=AGENT_CALL, tools=reg)
    task = asyncio.create_task(session.run())

    await until(lambda: bool(connector.opened) and connector.last.tool_responses != [])
    assert ran == []
    assert connector.last.tool_responses[0].response == {"error": "tool_not_available"}
    assert connector.last.tool_responses[0].scheduling == "WHEN_IDLE"

    await session.close()
    await task


async def test_a_failing_tool_tells_the_model_without_cutting_the_user_off() -> None:
    async def boom(**_: Any) -> dict[str, Any]:
        raise RuntimeError("the repo is gone")

    reg = SimpleToolRegistry()
    reg.add("code_build", boom)
    connector = ScriptedConnector(
        scripts=[[fake.tool_call("tc-4", "code_build")]], repeat_last=True
    )
    session = make_session(connector, tools=reg)
    task = asyncio.create_task(session.run())

    await until(lambda: bool(connector.opened) and connector.last.tool_responses != [])
    response = connector.last.tool_responses[0]
    assert response.response["error"] == "RuntimeError"
    assert response.scheduling == "WHEN_IDLE"

    await session.close()
    await task


async def test_a_cancelled_tool_call_never_answers() -> None:
    gate = asyncio.Event()

    async def slow(**_: Any) -> dict[str, Any]:
        await gate.wait()
        return {"ok": True}

    reg = SimpleToolRegistry()
    reg.add("code_build", slow)
    pause = Pause()
    connector = ScriptedConnector(
        scripts=[
            [
                fake.tool_call("tc-5", "code_build"),
                pause,
                fake.ServerEvent(cancelled_tool_ids=("tc-5",)),
            ]
        ],
        repeat_last=True,
    )
    session = make_session(connector, tools=reg)
    task = asyncio.create_task(session.run())

    await until(lambda: session.tool_calls == 1)
    pause.event.set()
    await settle()
    assert connector.last.tool_responses == []

    gate.set()
    await settle()
    assert connector.last.tool_responses == []
    await session.close()
    await task


async def test_a_tool_call_with_no_registry_is_logged_not_crashed() -> None:
    events: list[LiveEvent] = []
    connector = ScriptedConnector(scripts=[[fake.tool_call("tc-6", "anything")]], repeat_last=True)
    session = make_session(connector, events=events.append)
    task = asyncio.create_task(session.run())
    await until(lambda: any(e.kind == "tool_unroutable" for e in events))
    await session.close()
    await task


# ─────────────────── the read-options handoff (SILENT + fallback) ───────────────────


async def test_announce_options_sends_the_prefill_before_the_silent_response() -> None:
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)

    session.gate_uplink("reader speaking")
    await session.announce_options(
        ToolCall(id="tc-7", name="read_options"),
        ["SQLite", "Postgres"],
        question="How should todos be stored?",
    )
    transport = connector.last
    # DESK does not claim SILENT is verified, so the documented fallback goes
    # out first — as a prefill, while the uplink is still gated.
    assert len(transport.sent_text) == 1
    body, role, turn_complete = transport.sent_text[0]
    assert "1. SQLite" in body and "2. Postgres" in body
    assert role == "user" and turn_complete is False
    response = transport.tool_responses[0]
    assert response.scheduling == "SILENT"
    assert response.response["already_read_aloud"] is True
    assert response.response["do_not_repeat"] is True
    assert response.response["options"] == [
        {"n": 1, "label": "SQLite"},
        {"n": 2, "label": "Postgres"},
    ]

    await session.close()
    await task


async def test_a_verified_profile_sends_only_the_silent_response() -> None:
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    profile = SessionProfile(name="desk", silent_scheduling_verified=True)
    session = make_session(connector, profile=profile)
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)

    session.gate_uplink()
    await session.announce_options(ToolCall(id="tc-8", name="read_options"), ["Yes", "No"])
    assert connector.last.sent_text == []
    assert connector.last.tool_responses[0].scheduling == "SILENT"

    await session.close()
    await task


async def test_announcing_options_with_the_uplink_open_is_refused() -> None:
    # send_client_content interleaved with send_realtime_input is documented as
    # producing unexpected results, and the reader is speaking: Gemini must not
    # hear it as the user.
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)

    with pytest.raises(RuntimeError, match="gated"):
        await session.announce_options(ToolCall(id="tc-9", name="read_options"), ["Yes"])
    with pytest.raises(RuntimeError, match="gate the uplink"):
        await session.prefill("anything")

    await session.close()
    await task


async def test_the_live_voice_refuses_exact_tier_text() -> None:
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)

    await session.say(Utterance(text="Claude Code has a question about storage."))
    assert connector.last.sent_text[0][2] is True
    with pytest.raises(FidelityViolation, match="paraphrases"):
        await session.say(
            Utterance(text="2. Postgres", fidelity="exact", label="Postgres", index=2)
        )

    await session.close()
    await task


async def test_sending_without_a_connection_raises_rather_than_vanishing() -> None:
    session = make_session(ScriptedConnector())
    with pytest.raises(NotConnected):
        await session.activity_start()
    with pytest.raises(NotConnected):
        await session.send_tool_response("tc-0", {"ok": True})


# ───────────────────────────── reconnect budget ─────────────────────────────


async def test_connect_failures_back_off_and_then_succeed() -> None:
    sleeper = Sleeper()
    connector = ScriptedConnector(scripts=[[fake.audio(b"hello")]], fail_times=2)
    sink = fake.BytesSink()
    session = make_session(connector, sink=sink, sleeper=sleeper)
    task = asyncio.create_task(session.run())

    await until(lambda: sink.audio() == b"hello")
    assert sleeper.asked == [0.25, 0.5]
    await session.close()
    await task


async def test_the_reconnect_budget_is_finite_and_says_so() -> None:
    connector = ScriptedConnector(fail_times=99)
    session = make_session(connector, max_failures=3)
    with pytest.raises(LiveDown, match="consecutive connect failures"):
        await session.run()
    assert len(connector.handles) == 3


async def test_no_credential_is_not_retried() -> None:
    class NoKey:
        async def open(self, *_: Any, **__: Any) -> Any:
            raise LiveUnavailable("no Gemini credential")

    session = make_session(NoKey())
    with pytest.raises(LiveUnavailable):
        await session.run()


async def test_the_real_connector_refuses_to_go_looking_for_a_key() -> None:
    with pytest.raises(LiveUnavailable, match="keyring"):
        await GenaiConnector().open(DESK)


async def test_reconnect_can_be_switched_off_for_a_one_shot_leg() -> None:
    connector = ScriptedConnector(scripts=[[fake.audio(b"x"), Hangup()]])
    session = make_session(connector, sink=fake.BytesSink(), reconnect=False)
    await session.run()
    assert len(connector.opened) == 1


# ───────────────────────── multi-instantiability ─────────────────────────


async def test_two_sessions_share_nothing() -> None:
    desk_conn = ScriptedConnector(scripts=[[fake.handle("desk-h"), Pause()]], repeat_last=True)
    call_conn = ScriptedConnector(scripts=[[fake.handle("call-h"), Pause()]], repeat_last=True)
    desk = make_session(desk_conn, profile=DESK, sink=fake.BytesSink())
    call = make_session(call_conn, profile=AGENT_CALL, sink=fake.BytesSink())
    tasks = [asyncio.create_task(desk.run()), asyncio.create_task(call.run())]

    await until(lambda: desk.resume_handle == "desk-h" and call.resume_handle == "call-h")
    assert desk.profile.voice != call.profile.voice
    assert desk.profile.tools != call.profile.tools
    assert desk_conn.voices == ["Zephyr"] and call_conn.voices == ["Charon"]

    await asyncio.gather(desk.close(), call.close())
    await asyncio.gather(*tasks)


# ───────────────────────── the spine: bus and ledger ─────────────────────────


async def test_session_events_reach_the_bus_through_a_connection_holding_drain(
    con: sqlite3.Connection,
) -> None:
    events = QueuedLiveEvents()
    connector = ScriptedConnector(
        scripts=[
            [fake.handle("h-bus"), fake.audio(b"x"), fake.go_away(3.0), fake.turn_complete()],
            [],
        ]
    )
    session = make_session(connector, sink=fake.BytesSink(), events=events)
    task = asyncio.create_task(session.run())
    await until(lambda: len(connector.opened) == 2)
    await session.close()
    await task

    published = events.drain(con, "desk")
    assert published > 0
    kinds = [r["kind"] for r in con.execute("SELECT kind FROM events ORDER BY seq")]
    assert "live.connected" in kinds
    assert "live.go_away" in kinds
    assert "live.handle" in kinds
    assert bus.verify_chain(con) is None  # None means the chain is intact

    # A resumption handle is a connection credential: only its presence is
    # logged, never its value.
    payloads = " ".join(str(r["payload"]) for r in con.execute("SELECT payload FROM events"))
    assert "h-bus" not in payloads


async def test_the_event_queue_drops_the_oldest_and_counts_it() -> None:
    events = QueuedLiveEvents(limit=2)
    for i in range(5):
        events(LiveEvent(kind="tick", at=float(i)))
    assert events.dropped == 3
    assert [e.at for e in events.pending()] == [3.0, 4.0]


def test_usage_lands_in_the_ledger_unpriced(con: sqlite3.Connection) -> None:
    record_usage(con, Usage(total_tokens=1234, prompt_tokens=1000, response_tokens=234))
    record_session_time(con, 42.5)
    rows = list(con.execute("SELECT provider, unit, amount, usd_equiv FROM spend ORDER BY unit"))
    assert [(r["provider"], r["unit"], r["amount"]) for r in rows] == [
        ("gemini", "gemini_sec", 42.5),
        ("gemini", "tokens", 1234.0),
    ]
    # No price: the only figure available is third-party and its primary source
    # was egress-blocked, so the ledger says "unpriced" out loud instead.
    assert all(r["usd_equiv"] is None for r in rows)


# ───────────────── reading what the SDK actually sends ─────────────────


def test_server_event_reads_the_real_sdk_message_shape() -> None:
    from google.genai import types as t

    msg = t.LiveServerMessage(
        server_content=t.LiveServerContent(
            model_turn=t.Content(
                role="model",
                parts=[
                    t.Part(inline_data=t.Blob(data=b"\x01\x02", mime_type="audio/pcm;rate=24000")),
                    t.Part(text="hello"),
                ],
            ),
            output_transcription=t.Transcription(text="jarvis full stop"),
            input_transcription=t.Transcription(text="next"),
            turn_complete=True,
        ),
        usage_metadata=t.UsageMetadata(
            total_token_count=90,
            prompt_token_count=60,
            response_token_count=30,
            prompt_tokens_details=[t.ModalityTokenCount(modality="AUDIO", token_count=55)],
            response_tokens_details=[t.ModalityTokenCount(modality="AUDIO", token_count=30)],
        ),
    )
    event = server_event(msg)
    assert event.audio == b"\x01\x02"
    assert event.text == "hello"
    assert event.output_transcript == "jarvis full stop"
    assert event.input_transcript == "next"
    assert event.turn_complete is True
    assert event.usage is not None
    assert event.usage.total_tokens == 90
    assert event.usage.by_modality == {"in:AUDIO": 55, "out:AUDIO": 30}


def test_server_event_reads_tool_calls_goaway_and_resumption() -> None:
    from google.genai import types as t

    event = server_event(
        t.LiveServerMessage(
            tool_call=t.LiveServerToolCall(
                function_calls=[
                    t.FunctionCall(id="tc-1", name="answer_question", args={"picks": [1]})
                ]
            ),
            go_away=t.LiveServerGoAway(time_left="10s"),
            session_resumption_update=t.LiveServerSessionResumptionUpdate(
                new_handle="h-real", resumable=True
            ),
        )
    )
    assert event.tool_calls == (ToolCall(id="tc-1", name="answer_question", args={"picks": [1]}),)
    assert event.go_away_s == 10.0
    assert event.resumption_handle == "h-real"
    assert event.resumable is True


def test_an_empty_message_translates_to_an_empty_event() -> None:
    from google.genai import types as t

    event = server_event(t.LiveServerMessage())
    assert event == server_event(t.LiveServerMessage())
    assert event.audio == b"" and event.tool_calls == () and event.usage is None


def test_tool_result_refuses_an_invented_scheduling() -> None:
    with pytest.raises(ValueError, match="FunctionResponseScheduling"):
        ToolResult({"ok": True}, "LOUDLY")
    assert ToolResult({"ok": True}).scheduling == "SILENT"


async def test_a_sink_that_throws_costs_a_reconnect_not_the_conversation() -> None:
    class BrokenOnce:
        def __init__(self) -> None:
            self.calls = 0

        async def write(self, pcm: bytes, *, tier: str = "free") -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("the mixer track is gone")

    connector = ScriptedConnector(
        scripts=[[fake.handle("h-sink"), fake.audio(b"x")], [fake.audio(b"y")]]
    )
    events: list[LiveEvent] = []
    session = make_session(connector, sink=BrokenOnce(), events=events.append)
    task = asyncio.create_task(session.run())

    await until(lambda: len(connector.opened) == 2)
    assert connector.handles == [None, "h-sink"]
    assert any(e.kind == "stream_error" for e in events)

    await session.close()
    await task


# ──────────────── the reconnects nobody on the far side asks for ────────────────


async def test_a_voice_change_on_a_silent_session_still_reconnects() -> None:
    # THE COMMON CASE IS SILENCE. Between turns the server sends nothing, so a
    # receive loop that only re-examines its own state after a server message
    # would hold the old voice — and the old connection — until the model
    # happened to speak again. Nothing here unblocks the script.
    connector = ScriptedConnector(scripts=[[fake.handle("h-quiet")], [fake.audio(b"y")]])
    session = make_session(connector, sink=fake.BytesSink())
    task = asyncio.create_task(session.run())
    await until(lambda: session.resume_handle == "h-quiet")

    await session.change_voice("Puck")
    await until(lambda: len(connector.opened) == 2)
    assert connector.handles == [None, "h-quiet"]
    assert connector.voices == ["Zephyr", "Puck"]

    await session.close()
    await task


async def test_a_dead_uplink_says_so_and_costs_a_reconnect_not_the_conversation() -> None:
    # The send side usually notices a dead socket first: a write fails while
    # receive() waits for a message that will never come. A pump that merely
    # ended would leave a session that looks connected and hears nothing.
    class DeafOnce(fake.ScriptedTransport):
        fail = True

        async def send_audio(self, data: bytes, *, mime_type: str) -> None:
            if type(self).fail:
                type(self).fail = False
                raise ConnectionResetError("socket gone on the send side")
            await super().send_audio(data, mime_type=mime_type)

    class Conn(ScriptedConnector):
        async def open(self, profile: Any, *, handle: Any = None, declarations: Any = None) -> Any:
            self.handles.append(handle)
            self.voices.append(profile.voice)
            transport = DeafOnce([fake.handle("h-up"), Pause()])
            self.opened.append(transport)
            return transport

    DeafOnce.fail = True
    events: list[LiveEvent] = []
    source = FrameSource([b"\x01\x02" * 160], rate=16_000, block=320)
    connector = Conn()
    session = make_session(connector, source=source, events=events.append)
    task = asyncio.create_task(session.run())

    await until(lambda: len(connector.opened) == 2)
    assert any(e.kind == "uplink_error" for e in events)
    assert session.uplink_errors == 1
    assert connector.handles == [None, "h-up"]  # the conversation survives it

    source.feed(b"\x03\x04" * 160)
    await until(lambda: connector.last.sent_audio != [])
    await session.close()
    await task


async def test_an_uplink_that_never_recovers_gives_up_instead_of_looping_forever() -> None:
    class AlwaysDeaf(fake.ScriptedTransport):
        async def send_audio(self, data: bytes, *, mime_type: str) -> None:
            raise ConnectionResetError("the microphone half of this socket is gone")

    class Conn(ScriptedConnector):
        async def open(self, profile: Any, *, handle: Any = None, declarations: Any = None) -> Any:
            self.handles.append(handle)
            transport = AlwaysDeaf([fake.handle("h-loop"), Pause()])
            self.opened.append(transport)
            return transport

    source = FrameSource([b"\x01\x02" * 160] * 20)
    session = make_session(Conn(), source=source, max_failures=3)
    with pytest.raises(LiveDown):
        await session.run()


async def test_numpy_int16_frames_go_up_as_bytes_without_importing_numpy_here() -> None:
    # The real desk source hands over int16 arrays, not bytes. The session
    # serialises them without ever importing numpy itself, which is what keeps
    # this package importable on a machine with no audio stack at all.
    np = pytest.importorskip("numpy")
    tone = (np.sin(2 * np.pi * 440 * np.arange(320) / 16_000) * 12_000).astype(np.int16)
    source = FrameSource([ActivityMark.START, tone, ActivityMark.END])
    connector = ScriptedConnector(scripts=[[Pause()]])
    session = make_session(connector, source=source)
    task = asyncio.create_task(session.run())

    await until(lambda: connector.opened and len(connector.last.marks) == 2)
    transport = connector.last
    assert transport.sent_audio == [tone.tobytes()]
    assert len(transport.sent_audio[0]) == 640  # 320 samples, int16 LE
    assert transport.sent_mime == ["audio/pcm;rate=16000"]
    assert np.frombuffer(transport.sent_audio[0], dtype=np.int16).max() > 11_000

    await session.close()
    await task


def test_drain_hands_the_bus_a_redactor_so_transcripts_are_not_kept_raw(
    con: sqlite3.Connection,
) -> None:
    # Session payloads carry what the user said and what Jarvis said back into a
    # log that is kept forever. The bus cannot know what a secret looks like, so
    # the redactor has to arrive from here.
    events = QueuedLiveEvents()
    events(LiveEvent(kind="input_transcript", at=0.0, detail={"text": "my key is sk-live-hunter2"}))
    assert events.drain(con, "desk", redactor=bus.Redactor.of(["sk-live-hunter2"])) == 1
    payloads = " ".join(str(r["payload"]) for r in con.execute("SELECT payload FROM events"))
    assert "sk-live-hunter2" not in payloads
    assert bus.REDACTED in payloads


async def test_the_spoken_ordinal_wins_over_a_number_the_caller_brought() -> None:
    # The answer path is symmetrically model-free: the model may emit an INDEX,
    # never a label, and the index it emits is resolved locally against what the
    # reader actually said. A caller's own "n" disagreeing with the spoken
    # ordinal would silently rebind an answer key to the wrong option.
    connector = ScriptedConnector(scripts=[[Pause()]], repeat_last=True)
    session = make_session(connector)
    task = asyncio.create_task(session.run())
    await until(lambda: session.connected)

    session.gate_uplink()
    await session.announce_options(
        ToolCall(id="tc-n", name="read_options"),
        [{"label": "SQLite", "n": 7}, {"label": "Postgres", "n": 9}],
    )
    options = connector.last.tool_responses[0].response["options"]
    assert [row["n"] for row in options] == [1, 2]
    body = connector.last.sent_text[0][0]
    assert "1. SQLite" in body and "2. Postgres" in body

    await session.close()
    await task
