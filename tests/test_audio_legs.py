"""The three legs, and the bench's decision rule.

The DeskLeg tests are the interesting ones: it has to be constructible, its
device selection has to be drivable with a fake probe, and its AEC has to degrade
LOUDLY rather than silently — all on a machine where PortAudio does not exist.
If any of that needed a sound card, the desk leg would only ever be testable on
the desk, which is where bugs are most expensive to find.

The bench's decision rule is tested here rather than in the tool because a
threshold that only exists inside a script nobody runs in CI is a threshold that
drifts. These numbers were fixed before the measurement on purpose.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio import BLOCK, DEV_RATE, MIC_RATE, VAD_FRAME
from jarvis.audio.devices import ClockSplit, DeviceInfo, DeviceVanished
from jarvis.audio.dsp import EnergyVad, NullEchoCanceller
from jarvis.audio.graph import AudioGraph
from jarvis.audio.legs import DeskLeg, ListSink, PhoneLeg, SyntheticLeg, frames_from
from jarvis.audio.micbus import MicBus
from jarvis.audio.mixer import PlaybackMixer, Prio, SecondOutputStream
from jarvis.audio.turn import RecordingUplink, TurnController
from tools.aec_bench import (
    BARGE_IN_REQUIRED,
    BARGE_IN_TRIALS,
    Result,
    count_false_barge_ins,
    decide,
    score_erle,
)

HEADSET = DeviceInfo(0, "Jabra Evolve2 40", "ALSA", 1, 2, 48000.0)
WEBCAM = DeviceInfo(1, "HD Pro Webcam C920", "ALSA", 2, 0, 32000.0)
MONITOR = DeviceInfo(2, "HDMI Output", "ALSA", 0, 2, 48000.0)


class FakeProbe:
    def __init__(self, devices: list[DeviceInfo], defaults: tuple[int | None, int | None]) -> None:
        self._devices, self._defaults = devices, defaults

    def devices(self) -> list[DeviceInfo]:
        return self._devices

    def defaults(self) -> tuple[int | None, int | None]:
        return self._defaults


def graph_for(rate: int, block: int) -> AudioGraph:
    mix = PlaybackMixer(rate=rate)
    return AudioGraph(
        mixer=mix,
        micbus=MicBus(rate=MIC_RATE, seconds=2.0),
        turn=TurnController(
            mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=RecordingUplink()
        ),
        device_rate=rate,
        block=block,
    )


# -- the desk leg, with no sound card -----------------------------------------


def test_the_desk_leg_selects_a_duplex_device_through_an_injected_probe() -> None:
    leg = DeskLeg(probe=FakeProbe([HEADSET, WEBCAM, MONITOR], (0, 0)))
    sel = leg.select()
    assert sel.index == 0 and sel.is_system_default
    assert leg.device_rate == DEV_RATE and leg.block == BLOCK
    assert leg.confirm_ms == 200
    assert leg.has_hardware_echo_control is False, "-> the TurnController must duck-confirm"


def test_the_desk_leg_refuses_a_split_clock_before_opening_anything() -> None:
    leg = DeskLeg(probe=FakeProbe([HEADSET, WEBCAM, MONITOR], (1, 2)))
    with pytest.raises(ClockSplit):
        leg.select()


def test_the_desk_leg_says_which_device_went_missing() -> None:
    leg = DeskLeg(device_name="Evolve2", probe=FakeProbe([WEBCAM, MONITOR], (1, 2)))
    with pytest.raises(DeviceVanished):
        leg.select()


def test_the_desk_leg_degrades_to_no_aec_loudly_rather_than_silently() -> None:
    """pywebrtc-audio is not installed here. What is lost is open-speaker
    barge-in and nothing else — but the leg must SAY it degraded."""
    leg = DeskLeg(probe=FakeProbe([HEADSET], (0, 0)))
    aec = leg.make_aec()
    assert isinstance(aec, NullEchoCanceller)
    assert leg.aec_degraded is True


def test_a_leg_that_requires_aec_refuses_instead() -> None:
    leg = DeskLeg(probe=FakeProbe([HEADSET], (0, 0)), require_aec=True)
    with pytest.raises(Exception) as exc:
        leg.make_aec()
    assert "pywebrtc-audio" in str(exc.value)


class FakeStream:
    """A stream that behaves like PortAudio's: opened stopped, fires only once started.

    The real ``sd.Stream`` calls ``Pa_OpenStream`` in ``__init__`` and
    ``Pa_StartStream`` only inside ``start()``. A fake that ran the callback
    regardless would pass the buggy version of :meth:`DeskLeg.open`, which is
    exactly how this shipped — so the fake refuses to fire until started, and
    that refusal is the whole point of it.
    """

    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class Tinfo:
    inputBufferAdcTime = 0.0  # noqa: N815 - PortAudio's own field names
    outputBufferDacTime = 0.0  # noqa: N815


def open_capturing(leg: DeskLeg, graph: AudioGraph, monkeypatch, stream=None):
    """Open the leg against a fake device, and hand back (stream, callback)."""
    made = stream if stream is not None else FakeStream()
    captured = {}

    def fake_open(selection, callback, *, block=BLOCK, channels=1):
        captured["callback"] = callback
        return made

    monkeypatch.setattr("jarvis.audio.legs.open_duplex_stream", fake_open)
    leg.open(graph)
    return made, captured["callback"]


def drive(callback, graph: AudioGraph, blocks: int, *, started: bool) -> None:
    """Feed the callback the way PortAudio would — or, when stopped, not at all."""
    out = np.zeros((BLOCK, 1), np.int16)
    for _ in range(blocks):
        if not started:
            continue  # what a stopped device does: no callback, no sound, no error
        callback(np.zeros((BLOCK, 1), np.int16), out, BLOCK, Tinfo(), None)


def test_the_duplex_stream_is_actually_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE bug that made `python -m jarvis desk` deaf and mute for one release.

    ``open_duplex_stream`` returns an unstarted stream by design — that gap is
    where the mixer claim goes — and for one release nothing ever closed it.
    Every other audio test drives ``graph.step`` directly, so the whole suite
    was green while the only thing that calls it in production, the PortAudio
    callback, was never armed.
    """
    graph = graph_for(DEV_RATE, BLOCK)
    leg = DeskLeg(probe=FakeProbe([HEADSET], (0, 0)))
    stream, _ = open_capturing(leg, graph, monkeypatch)
    assert stream.started, "the device was opened and never started: silence in both directions"


def test_an_unstarted_stream_loses_the_microphone_and_the_speaker_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consequence, asserted rather than assumed.

    ``graph.step`` is the only caller of ``mixer.pull``, so a callback that never
    fires costs capture AND playback — and reports neither, because an assistant
    that hears nothing sounds exactly like an assistant nobody is talking to.
    """
    graph = graph_for(DEV_RATE, BLOCK)
    leg = DeskLeg(probe=FakeProbe([HEADSET], (0, 0)))
    _, callback = open_capturing(leg, graph, monkeypatch)

    drive(callback, graph, 5, started=False)
    assert graph.blocks == 0

    drive(callback, graph, 5, started=True)
    assert graph.blocks == 5, "a started stream must drive the graph"
    assert graph.micbus.written > 0, "and the microphone must reach the bus"


def test_a_stream_that_will_not_start_releases_the_mixer_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the retry reports "something else is already using the speaker"."""

    class WontStart(FakeStream):
        def start(self) -> None:
            raise OSError("PortAudio: device unavailable")

    graph = graph_for(DEV_RATE, BLOCK)
    leg = DeskLeg(probe=FakeProbe([HEADSET], (0, 0)))
    with pytest.raises(OSError, match="device unavailable"):
        open_capturing(leg, graph, monkeypatch, stream=WontStart())

    graph.mixer.claim_output("desk-2").release()  # free, so the claim really went back


def test_opening_two_desk_legs_on_one_mixer_is_refused_at_the_claim() -> None:
    """Rule 2: the refusal happens BEFORE PortAudio is asked for the device, so a
    second leg fails on a check rather than on a driver error."""
    graph = graph_for(DEV_RATE, BLOCK)
    a = DeskLeg(name="desk-a", probe=FakeProbe([HEADSET], (0, 0)))
    b = DeskLeg(name="desk-b", probe=FakeProbe([HEADSET], (0, 0)))
    graph.mixer.claim_output(a.name)
    with pytest.raises(SecondOutputStream):
        b.open(graph)


# -- the phone leg ------------------------------------------------------------


def test_the_phone_leg_is_three_boxes_swapped() -> None:
    leg = PhoneLeg(lambda: None)
    assert leg.device_rate == MIC_RATE
    assert leg.confirm_ms == 0
    assert leg.has_hardware_echo_control is True
    assert isinstance(leg.make_aec(), NullEchoCanceller)
    assert leg.make_ns().level == 2, "NS level 2 + AGC on: PSTN levels vary by 30 dB"


def test_the_phone_leg_stops_cleanly_when_the_track_ends_mid_call() -> None:
    frames = iter([np.zeros(320, dtype=np.int16)] * 3)
    sink = ListSink(rate=MIC_RATE)
    leg = PhoneLeg(lambda: next(frames, None), sink, block=320)
    assert leg.run(graph_for(MIC_RATE, 320)) == 3
    assert len(sink.blocks) == 3


# -- the synthetic leg --------------------------------------------------------


def test_the_synthetic_leg_needs_no_device_no_network_and_no_key() -> None:
    graph = graph_for(MIC_RATE, 320)
    leg = SyntheticLeg(rate=MIC_RATE, block=320)
    leg.silence(5)
    assert len(leg.run(graph)) == 5
    assert graph.blocks == 5
    assert graph.elapsed == pytest.approx(5 * 320 / MIC_RATE)


def test_the_synthetic_leg_feeds_the_far_signal_back_as_a_delayed_echo() -> None:
    """The echo is the mixer's ACTUAL output, delayed — which is what a room does."""
    graph = graph_for(MIC_RATE, 320)
    graph.mixer.track("live", Prio.LIVE, ttl_s=None).write(
        np.full(3200, 10000, dtype=np.int16), at=0.0
    )
    leg = SyntheticLeg(rate=MIC_RATE, block=320, echo_gain=0.5, echo_delay_blocks=1)
    leg.silence(6)
    leg.run(graph)
    # Block 0 played 10000; block 1's near path therefore contains 5000 of echo,
    # and the mic bus (no AEC) carries it.
    captured = graph.micbus.reader("audit", from_start=True).read(320 * 3, timeout=0.0)
    assert captured is not None
    assert int(np.abs(captured[320:640]).max()) == pytest.approx(5000, abs=50)


def test_frames_from_drops_a_short_tail_rather_than_padding_it() -> None:
    """A padded final block is silence the graph never captured, and it lands in
    the hangover count as if the user had stopped talking."""
    out = frames_from(np.zeros(1000, dtype=np.int16), 320)
    assert len(out) == 3
    assert all(f.shape[0] == 320 for f in out)


def test_running_out_of_frames_is_not_an_error() -> None:
    graph = graph_for(MIC_RATE, 320)
    leg = SyntheticLeg(rate=MIC_RATE, block=320)
    leg.silence(2)
    assert len(leg.run(graph, blocks=10)) == 2
    assert leg.run(graph) == []


# -- the bench's decision rule ------------------------------------------------


def base_result(**over: float | int) -> Result:
    fields: dict[str, float | int] = {
        "median_erle_db": 28.0,
        "erle_first_half_db": 28.0,
        "erle_second_half_db": 28.0,
        "scored_frames": 800,
        "barge_ins_detected": BARGE_IN_REQUIRED,
        "barge_in_trials": BARGE_IN_TRIALS,
        "median_detect_ms": 180.0,
        "false_barge_ins": 0,
        "seconds": 600.0,
    }
    fields.update(over)
    return Result(**fields)  # type: ignore[arg-type]


def test_the_ship_branch_needs_all_three_conditions() -> None:
    assert decide(base_result())[0] == "open_speakers"
    # ERLE alone is not enough.
    assert decide(base_result(barge_ins_detected=17))[0] == "cap_and_remeasure"
    assert decide(base_result(false_barge_ins=40))[0] == "cap_and_remeasure"


def test_the_middle_band_caps_and_remeasures() -> None:
    verdict, why = decide(base_result(median_erle_db=19.0))
    assert verdict == "cap_and_remeasure"
    assert "-6 dB" in why and "onset_frames = 4" in why


def test_below_fifteen_db_walks_the_ladder_to_a_headset() -> None:
    verdict, why = decide(base_result(median_erle_db=12.0))
    assert verdict == "walk_the_ladder"
    assert "headset" in why


def test_degrading_erle_is_diagnosed_as_clock_drift_not_as_a_bad_room() -> None:
    """The one failure whose fix is completely different from every other one."""
    verdict, why = decide(
        base_result(median_erle_db=26.0, erle_first_half_db=30.0, erle_second_half_db=21.0)
    )
    assert verdict == "walk_the_ladder"
    assert "CLOCK DRIFT" in why and "crystals" in why


def test_erle_is_scored_only_over_frames_where_the_far_end_was_loud() -> None:
    """Otherwise a canceller that does nothing scores well on the silence."""
    rate = DEV_RATE
    loud = (np.sin(np.arange(rate) * 0.05) * 9000).astype(np.int16)
    far = np.concatenate([np.zeros(rate * 4, dtype=np.int16), loud])
    near = far.copy()
    clean = (near.astype(np.float64) * 10 ** (-24 / 20)).astype(np.int16)
    median, _, _, frames = score_erle(near, clean, far, rate=rate)
    assert median == pytest.approx(24.0, abs=1.0)
    # 3 s of convergence discarded, 1 s of silence skipped: only the loud second scores.
    assert frames == pytest.approx(rate // BLOCK, abs=2)


def test_the_false_barge_in_count_runs_the_whole_duck_confirm_chain() -> None:
    """Not a VAD-firing count: a firing that the confirm window rejects costs a
    200 ms dip and is not a false barge-in."""
    rate = MIC_RATE
    silence = np.zeros(rate * 2, dtype=np.int16)
    starts, seconds = count_false_barge_ins(silence, silence, rate=rate)
    assert starts == 0
    assert seconds == pytest.approx(2.0)

    speech = (np.sin(np.arange(rate * 2) * 0.2) * 9000).astype(np.int16)
    starts, _ = count_false_barge_ins(speech, silence, rate=rate)
    assert starts == 1, "a real voice into a silent room IS a turn, not a false positive"
