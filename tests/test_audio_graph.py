"""The whole graph, driven by synthetic audio, with no device and no API key.

This is the file that proves the design claim: one code path above ``clean16``,
exercised end to end by arrays. It runs the desk rates (48 kHz in, soxr down to
16 kHz) and the phone rates (16 kHz straight through) through the SAME
:class:`AudioGraph`, and asserts that the only differences are the front end and
which detectors are armed.

The echo test is the one worth reading. ``SyntheticLeg`` can feed the mixer's own
output back into the near path at a chosen gain and delay — which is what a room
does — so "a barge-in that turns out to be echo" is not a mocked VAD answer here
but an actual acoustic loop that the duck-confirm chain has to see through.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from jarvis.audio import BLOCK, DEV_RATE, MIC_RATE, VAD_FRAME
from jarvis.audio.dsp import EnergyVad, NullEchoCanceller, NullResampler, erle_db
from jarvis.audio.graph import AudioEvent, AudioGraph, QueuedEventSink, ReferenceDesync
from jarvis.audio.legs import DESK_DETECTORS, PHONE_DETECTORS, ListSink, PhoneLeg, SyntheticLeg
from jarvis.audio.micbus import MicBus
from jarvis.audio.mixer import PlaybackMixer, Prio
from jarvis.audio.turn import RecordingUplink, TurnController, TurnState
from jarvis.db import connect, migrate

PHONE_BLOCK = 320  # 20 ms at 16 kHz


def speech(n: int, amp: int = 7000) -> np.ndarray:
    """A loud, band-limited-ish signal. Loud enough for the energy VAD, and
    structured rather than white so a resampler has something to preserve."""
    t = np.arange(n)
    wave = np.sin(t * 2 * np.pi * 220 / MIC_RATE) + 0.5 * np.sin(t * 2 * np.pi * 700 / MIC_RATE)
    return (wave / 1.5 * amp).astype(np.int16)


def quiet(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.int16)


def blocks(pcm: np.ndarray, size: int) -> list[np.ndarray]:
    n = (pcm.shape[0] // size) * size
    return [pcm[i : i + size] for i in range(0, n, size)]


def build(
    *,
    rate: int = MIC_RATE,
    block: int = PHONE_BLOCK,
    confirm_ms: int = 200,
    echo_gain: float = 0.0,
    threshold_dbfs: float = -45.0,
) -> tuple[AudioGraph, SyntheticLeg, PlaybackMixer, RecordingUplink, list[AudioEvent]]:
    mix = PlaybackMixer(rate=rate)
    bus = MicBus(rate=MIC_RATE, seconds=4.0)
    up = RecordingUplink()
    events: list[AudioEvent] = []
    turn = TurnController(
        mixer=mix,
        vad=EnergyVad(frame_samples=VAD_FRAME, threshold_dbfs=threshold_dbfs),
        uplink=up,
        confirm_ms=confirm_ms,
    )
    leg = SyntheticLeg(rate=rate, block=block, confirm_ms=confirm_ms, echo_gain=echo_gain)
    graph = AudioGraph(
        mixer=mix,
        micbus=bus,
        turn=turn,
        aec=leg.make_aec(),
        device_rate=rate,
        block=block,
        on_event=events.append,
    )
    turn._on_event = graph.on_turn_event  # noqa: SLF001 - wiring, not behaviour
    return graph, leg, mix, up, events


# -- rule 2: the reference IS the output --------------------------------------


def test_the_aec_reference_is_the_literal_array_handed_to_the_device() -> None:
    graph, leg, mix, _, _ = build()
    mix.track("live", Prio.LIVE).write(speech(PHONE_BLOCK * 20), at=0.0)
    leg.feed(blocks(quiet(PHONE_BLOCK * 10), PHONE_BLOCK))
    played = leg.run(graph)
    assert played
    graph.assert_reference_is_output()
    # The device gets the object the mixer produced — not a copy, not a re-mix.
    assert played[-1] is mix.last_pull()
    # The leg's recording IS a copy, so keeping a log of what was played cannot
    # accidentally alias the live buffer.
    assert leg.played[-1] is not mix.last_pull()
    np.testing.assert_array_equal(leg.played[-1], mix.last_pull())


def test_a_reference_taken_from_anywhere_else_is_caught() -> None:
    """A canceller that rebuilds the reference from the tracks would pass an
    equality check and be silently wrong, so the invariant is identity."""

    class RebuildingAec(NullEchoCanceller):
        def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
            return super().process(near, far.copy())

    mix = PlaybackMixer(rate=MIC_RATE)
    bus = MicBus(rate=MIC_RATE, seconds=1.0)
    turn = TurnController(
        mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=RecordingUplink()
    )
    graph = AudioGraph(
        mixer=mix,
        micbus=bus,
        turn=turn,
        aec=RebuildingAec(MIC_RATE),
        device_rate=MIC_RATE,
        block=PHONE_BLOCK,
    )
    graph.step(quiet(PHONE_BLOCK))
    # The graph itself still holds the right object, so the invariant holds here;
    # what a rogue path would break is the mixer's own record.
    graph.assert_reference_is_output()
    mix.pull(PHONE_BLOCK, at=1.0)  # somebody else pulled: now they disagree
    with pytest.raises(ReferenceDesync):
        graph.assert_reference_is_output()


# -- rule 3: one tap, many cursors --------------------------------------------


def test_every_detector_reads_the_same_tap_with_its_own_cursor() -> None:
    graph, leg, _, _, _ = build()
    wake = graph.reader("wake")
    kill = graph.reader("kill")
    nav = graph.reader("nav")
    leg.feed(blocks(speech(PHONE_BLOCK * 10), PHONE_BLOCK))
    leg.run(graph)

    assert wake.available == kill.available == nav.available == PHONE_BLOCK * 10
    wake.read(1600, timeout=0.0)
    assert kill.available == PHONE_BLOCK * 10, "one reader's progress is nobody else's"
    assert set(graph.readers) == {"vad", "wake", "kill", "nav"}
    with pytest.raises(ValueError):
        graph.reader("wake")


def test_a_slow_detector_is_reported_rather_than_corrupted() -> None:
    """The uplink blocks on a socket for two seconds. It loses audio and is told."""
    mix = PlaybackMixer(rate=MIC_RATE)
    bus = MicBus(rate=MIC_RATE, seconds=0.5)  # a deliberately tiny ring
    turn = TurnController(
        mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=RecordingUplink()
    )
    events: list[AudioEvent] = []
    graph = AudioGraph(
        mixer=mix,
        micbus=bus,
        turn=turn,
        device_rate=MIC_RATE,
        block=PHONE_BLOCK,
        on_event=events.append,
    )
    slow = graph.reader("uplink")
    leg = SyntheticLeg(rate=MIC_RATE, block=PHONE_BLOCK)
    leg.feed(blocks(speech(PHONE_BLOCK * 60), PHONE_BLOCK))
    leg.run(graph)

    # 60 blocks went past a 0.5 s ring. The writer never waited, the reader can
    # only be served what is still there, and it is TOLD how far behind it got.
    assert slow.available == bus.capacity
    assert slow.behind > bus.capacity
    got = slow.read(VAD_FRAME, timeout=0.0)
    assert got is not None
    assert slow.lagging and slow.dropped > 0
    # The VAD reader keeps up, so it must NOT be reported as lagging.
    assert not any(e.kind == "mic.lagged" for e in events)


def test_a_lagging_vad_is_reported_once_per_gap_not_once_per_block() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    bus = MicBus(rate=MIC_RATE, seconds=0.2)
    turn = TurnController(
        mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=RecordingUplink()
    )
    events: list[AudioEvent] = []
    graph = AudioGraph(
        mixer=mix,
        micbus=bus,
        turn=turn,
        device_rate=MIC_RATE,
        block=PHONE_BLOCK * 20,
        on_event=events.append,
    )
    # One block bigger than the whole ring: the VAD cannot help but lose audio.
    graph.step(speech(PHONE_BLOCK * 20))
    graph.step(speech(PHONE_BLOCK * 20))
    lagged = [e for e in events if e.kind == "mic.lagged"]
    assert lagged, "a lost frame must produce a line somebody can find later"
    assert all(e.detail["dropped"] > 0 for e in lagged)
    assert lagged[-1].detail["dropped_total"] >= lagged[0].detail["dropped_total"]


# -- rule 5: one code path, two front ends ------------------------------------


def test_the_desk_front_end_resamples_48k_down_to_the_16k_tap() -> None:
    graph, leg, _, up, _ = build(rate=DEV_RATE, block=BLOCK)
    assert graph.micbus.rate == MIC_RATE
    leg.feed(blocks(speech(BLOCK * 50, amp=9000), BLOCK))
    leg.run(graph)
    # 50 blocks of 960 at 48 kHz is 1.0 s, which lands as ~1.0 s at 16 kHz.
    assert 0.9 * MIC_RATE <= graph.micbus.written <= MIC_RATE
    assert graph.turn.state is TurnState.USER_SPEAKING
    assert up.starts == 1


def test_the_phone_front_end_has_no_resampler_at_all() -> None:
    graph, _, _, _, _ = build(rate=MIC_RATE, block=PHONE_BLOCK)
    assert isinstance(graph._to_mic, NullResampler)  # noqa: SLF001
    assert graph.aec.erle_db == 0.0, "no AEC on a call; the carrier already cancels"


def test_the_legs_differ_only_in_their_armed_detector_set() -> None:
    assert "wake" in DESK_DETECTORS and "wake" not in PHONE_DETECTORS
    assert PHONE_DETECTORS < DESK_DETECTORS
    assert {"vad", "uplink"} <= PHONE_DETECTORS


def test_the_phone_leg_pumps_the_same_graph() -> None:
    graph, _, _, up, _ = build(rate=MIC_RATE, block=PHONE_BLOCK, confirm_ms=0)
    frames = iter(blocks(speech(PHONE_BLOCK * 10), PHONE_BLOCK))
    sink = ListSink(rate=MIC_RATE)
    leg = PhoneLeg(lambda: next(frames, None), sink, block=PHONE_BLOCK)
    assert leg.confirm_ms == 0
    assert leg.has_hardware_echo_control
    assert leg.run(graph) == 10
    assert up.starts == 1
    assert sink.audio().shape[0] == PHONE_BLOCK * 10


# -- the acoustic loop --------------------------------------------------------


# The residual echo a desk is EXPECTED to have: a canceller delivering the
# 15-20 dB the architecture asks of it leaves roughly this much of a 12000-peak
# far signal in the mic. The duck's 20 dB then has to finish the job.
RESIDUAL_ECHO_GAIN = 0.06


def test_a_real_echo_loop_is_rejected_by_the_confirm_window() -> None:
    """Jarvis speaks; the room feeds his own voice back. The chain must not commit.

    The loop is real: the near path literally contains a delayed, scaled copy of
    what the mixer played. The echo trips suspicion, the duck takes it 20 dB down,
    and 200 ms later the VAD has stopped — which is the entire design.
    """
    graph, leg, mix, up, events = build(echo_gain=RESIDUAL_ECHO_GAIN)
    leg.echo_delay_blocks = 1
    live = mix.track("live", Prio.LIVE, ttl_s=None)
    live.write(speech(MIC_RATE * 2, amp=12000), at=0.0)
    leg.feed(blocks(quiet(PHONE_BLOCK * 40), PHONE_BLOCK))  # the user says nothing
    leg.run(graph)

    assert up.starts == 0, "Jarvis interrupted himself"
    assert graph.turn.state is not TurnState.USER_SPEAKING
    assert any(e.kind == "barge_in.suspected" for e in events), "the echo did trip suspicion"
    assert any(e.kind == "barge_in.echo" for e in events), "and was then rejected"
    assert graph.turn.echo_rejections >= 1
    assert mix.is_playing, "so the utterance survived"


def test_an_uncancelled_echo_defeats_the_duck_which_is_why_aec_is_required() -> None:
    """The precondition, made falsifiable rather than assumed.

    With NO cancellation and a loud room the duck's 20 dB is not enough on its
    own and the chain commits a turn nobody asked for. That is not a bug in the
    chain: it is the measured reason the decision rule says 15 dB of ERLE is the
    floor and a wired headset is the recommended default. If this test ever
    passes with `starts == 0`, the duck-confirm numbers were quietly changed.
    """
    graph, leg, mix, up, _ = build(echo_gain=0.9)
    leg.echo_delay_blocks = 1
    mix.track("live", Prio.LIVE, ttl_s=None).write(speech(MIC_RATE * 2, amp=12000), at=0.0)
    leg.feed(blocks(quiet(PHONE_BLOCK * 40), PHONE_BLOCK))
    leg.run(graph)
    assert up.starts == 1, "20 dB of duck alone cannot reject a 0 dB ERLE room"


def test_a_real_voice_over_the_echo_loop_does_commit() -> None:
    """The same loop, but the user actually talks. This must NOT be rejected."""
    graph, leg, mix, up, _ = build(echo_gain=RESIDUAL_ECHO_GAIN)
    live = mix.track("live", Prio.LIVE, ttl_s=None)
    live.write(speech(MIC_RATE * 2, amp=8000), at=0.0)
    leg.feed(blocks(quiet(PHONE_BLOCK * 5), PHONE_BLOCK))
    leg.feed(blocks(speech(PHONE_BLOCK * 40, amp=11000), PHONE_BLOCK))
    leg.run(graph)

    assert up.starts == 1
    assert graph.turn.state is TurnState.USER_SPEAKING
    assert up.samples > 0


def test_the_duck_is_audible_in_what_was_actually_played() -> None:
    """Assert on the output blocks, not on internal state: the dip is the product."""
    graph, leg, mix, _, _ = build(echo_gain=0.0)
    live = mix.track("live", Prio.LIVE, ttl_s=None)
    live.write(speech(MIC_RATE * 2, amp=10000), at=0.0)
    leg.feed(blocks(quiet(PHONE_BLOCK * 3), PHONE_BLOCK))
    leg.feed(blocks(speech(PHONE_BLOCK * 6, amp=9000), PHONE_BLOCK))
    leg.run(graph)

    def rms(a: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(a.astype(np.float64)))))

    played = leg.played
    assert rms(played[1]) > 1000, "playback was at full level before the onset"
    # Somewhere after the onset the level drops by ~20 dB or the track is flushed.
    assert min(rms(b) for b in played[4:]) < rms(played[1]) / 5


def test_erle_measures_what_a_passthrough_canceller_actually_did() -> None:
    near = speech(16000, amp=8000)
    assert erle_db(near, near) == pytest.approx(0.0, abs=0.01)
    assert erle_db(near, (near * 0.1).astype(np.int16)) == pytest.approx(20.0, abs=0.5)
    assert erle_db(near, np.zeros(16000, dtype=np.int16)) == 0.0


# -- events reach the spine ---------------------------------------------------


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def test_audio_events_publish_to_the_bus_from_a_worker_not_the_callback(
    con: sqlite3.Connection,
) -> None:
    """publish() opens a write transaction; a BEGIN IMMEDIATE inside a 20 ms audio
    callback is a dropout you can hear. So the graph buffers and a worker drains."""
    sink = QueuedEventSink()
    mix = PlaybackMixer(rate=MIC_RATE)
    bus = MicBus(rate=MIC_RATE, seconds=1.0)
    up = RecordingUplink()
    turn = TurnController(
        mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=up, on_event=None
    )
    graph = AudioGraph(
        mixer=mix,
        micbus=bus,
        turn=turn,
        device_rate=MIC_RATE,
        block=PHONE_BLOCK,
        on_event=sink,
    )
    turn._on_event = graph.on_turn_event  # noqa: SLF001
    leg = SyntheticLeg(rate=MIC_RATE, block=PHONE_BLOCK)
    leg.feed(blocks(speech(PHONE_BLOCK * 10), PHONE_BLOCK))
    leg.run(graph)

    assert any(e.kind == "activity_start" for e in sink.pending())
    published = sink.drain(con, "jarvis-voice")
    assert published >= 1
    kinds = [
        r[0] for r in con.execute("SELECT kind FROM events WHERE kind LIKE 'audio.%'").fetchall()
    ]
    assert "audio.activity_start" in kinds
    assert sink.drain(con, "jarvis-voice") == 0, "draining twice must not republish"


def test_the_event_sink_drops_the_oldest_rather_than_stalling_the_audio_thread() -> None:
    sink = QueuedEventSink(limit=4)
    for i in range(10):
        sink(AudioEvent(kind="x", at=float(i)))
    assert len(sink.pending()) == 4
    assert sink.dropped == 6
    assert [e.at for e in sink.pending()] == [6.0, 7.0, 8.0, 9.0]


# -- multi-instantiability ----------------------------------------------------


def test_two_graphs_run_side_by_side() -> None:
    """A desk conversation and an outbound call, in one process, sharing nothing."""
    desk, desk_leg, desk_mix, desk_up, _ = build(rate=DEV_RATE, block=BLOCK)
    phone, phone_leg, _, phone_up, _ = build(rate=MIC_RATE, block=PHONE_BLOCK, confirm_ms=0)
    desk_leg.feed(blocks(speech(BLOCK * 20, amp=9000), BLOCK))
    phone_leg.feed(blocks(quiet(PHONE_BLOCK * 20), PHONE_BLOCK))
    desk_leg.run(desk)
    phone_leg.run(phone)
    assert desk_up.starts == 1
    assert phone_up.starts == 0
    assert desk.micbus is not phone.micbus


# -- the rate the mixer and the device must agree on ---------------------------


def test_a_mixer_at_the_wrong_rate_is_refused_rather_than_played_at_half_speed() -> None:
    """``PlaybackMixer()`` defaults to BUS_RATE and ``AudioGraph`` to DEV_RATE, so
    this is the pairing a caller falls into by accident. The array the mixer
    produces IS the device buffer and IS the AEC reference, so a mismatch plays
    at half speed and hands the canceller a reference the speaker never emitted —
    neither of which anything downstream can detect."""
    mix = PlaybackMixer()  # BUS_RATE = 24 kHz
    turn = TurnController(
        mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=RecordingUplink()
    )
    with pytest.raises(ValueError, match="24000"):
        AudioGraph(  # device_rate defaults to DEV_RATE = 48 kHz
            mixer=mix, micbus=MicBus(rate=MIC_RATE, seconds=1.0), turn=turn
        )


def test_the_live_ttl_fires_through_the_graph_without_an_explicit_stamp() -> None:
    """End to end, the way a real producer writes: no ``at``.

    ``jarvis/voice/router.py`` calls ``track.write(pcm, tier=...)`` with no
    timestamp. If the track stamped the wall clock while the graph pulls on its
    sample clock, held LIVE audio would never expire and a barge-in's late
    arrivals would be played after the interruption.
    """
    ticks = [0.0]
    mix = PlaybackMixer(rate=MIC_RATE, clock=lambda: ticks[0])
    bus = MicBus(rate=MIC_RATE, seconds=1.0)
    turn = TurnController(
        mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=RecordingUplink()
    )
    graph = AudioGraph(mixer=mix, micbus=bus, turn=turn, device_rate=MIC_RATE, block=PHONE_BLOCK)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    live = mix.track("live", Prio.LIVE)
    verbatim.write(speech(MIC_RATE * 10, amp=6000), tier="exact")
    live.write(speech(MIC_RATE * 3, amp=9000))

    for _ in range(200):  # 4 s of audio through the real graph
        ticks[0] = graph.elapsed
        graph.step(quiet(PHONE_BLOCK))

    assert live.dropped_stale > 0
    assert live.pending == 0
