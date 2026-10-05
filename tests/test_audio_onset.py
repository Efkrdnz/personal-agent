"""A breath must not open a turn: the idle onset that drops noise before anything is sent.

The old idle rule commits on three speech frames in a row and sends
``activity_start`` at once, and Gemini cannot take a turn back. These tests pin
the opt-in replacement the desk uses: K speech frames in any W, a longer idle
pre-roll that still carries the start of the word, a barge-in that keeps its own
rule and a shorter pre-roll, ``onset.rejected`` for what was dropped, and a
guard against a detector stuck on. Every frame is stamped from its index, as in
test_audio_turn.py, so nothing depends on the machine's speed.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio import (
    BARGE_PREROLL_MS,
    IDLE_ONSET,
    IDLE_ONSET_FALLBACK,
    IDLE_PREROLL_MS,
    MAX_TURN_S,
    MIC_RATE,
    VAD_FRAME,
)
from jarvis.audio.dsp import EnergyVad, SileroVad
from jarvis.audio.graph import AudioEvent, AudioGraph
from jarvis.audio.legs import SyntheticLeg
from jarvis.audio.micbus import MicBus
from jarvis.audio.mixer import PlaybackMixer, Prio
from jarvis.audio.turn import RecordingUplink, TurnController, TurnEvent, TurnState

FRAME_S = VAD_FRAME / MIC_RATE


class ScriptedVad:
    """Answers from a list; optionally a score and a level, as the real detectors report."""

    def __init__(self, script: list[bool], scores: list[float] | None = None) -> None:
        self.frame_samples = VAD_FRAME
        self.script = list(script)
        self.scores = scores
        self.i = 0
        self.resets = 0
        self.last_score = 0.0
        self.last_dbfs = -30.0

    def is_speech(self, frame: np.ndarray) -> bool:
        del frame
        if self.scores is not None and self.i < len(self.scores):
            self.last_score = self.scores[self.i]
        answer = self.script[self.i] if self.i < len(self.script) else self.script[-1]
        self.i += 1
        return answer

    def reset(self) -> None:
        self.resets += 1


def build(
    script: list[bool],
    *,
    speaking: bool = False,
    onset: tuple[int, int] | None = IDLE_ONSET,
    wake_window_s: float | None = None,
    max_turn_s: float | None = None,
    scores: list[float] | None = None,
) -> tuple[TurnController, PlaybackMixer, RecordingUplink, list[TurnEvent], ScriptedVad]:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE, ttl_s=None)
    if speaking:
        live.write(np.full(MIC_RATE * 5, 9000, dtype=np.int16))
    events: list[TurnEvent] = []
    up = RecordingUplink()
    vad = ScriptedVad(script, scores)
    turn = TurnController(
        mixer=mix,
        vad=vad,
        uplink=up,
        on_event=events.append,
        idle_onset=onset,
        idle_preroll_ms=IDLE_PREROLL_MS,
        preroll_ms=BARGE_PREROLL_MS,
        wake_window_s=wake_window_s,
        max_turn_s=max_turn_s,
    )
    return turn, mix, up, events, vad


def run(turn: TurnController, n: int, *, start: int = 0) -> None:
    for i in range(start, start + n):
        turn.feed(np.zeros(VAD_FRAME, dtype=np.int16), at=i * FRAME_S)


def kinds(events: list[TurnEvent]) -> list[str]:
    return [e.kind for e in events]


# -- the onset ----------------------------------------------------------------


def test_four_speech_frames_in_five_open_the_turn_on_the_fifth() -> None:
    turn, _, up, _, _ = build([True, False, True, True, True])
    run(turn, 4)
    assert up.starts == 0, "three of four is not enough"
    run(turn, 1, start=4)
    assert turn.state is TurnState.USER_SPEAKING and up.starts == 1


def test_a_three_frame_burst_opens_nothing_sends_nothing_and_is_logged() -> None:
    """The breath the old rule turned into a Gemini turn transcribed as "huh"."""
    turn, _, up, events, _ = build([False] * 3 + [True] * 3 + [False] * 8)
    run(turn, 14)
    assert up.starts == 0 and up.samples == 0, "nothing left the machine"
    assert turn.state is TurnState.IDLE
    rejected = [e for e in events if e.kind == "onset.rejected"]
    assert len(rejected) == 1 and turn.onsets_rejected == 1
    assert rejected[0].detail["speech_frames"] == 3
    assert rejected[0].detail["needed"] == list(IDLE_ONSET)


def test_the_same_burst_under_the_old_rule_did_open_a_turn() -> None:
    """The control: defaults are unchanged, which is why the desk must pass idle_onset."""
    turn, _, up, _, _ = build([False] * 3 + [True] * 3 + [False] * 8, onset=None)
    run(turn, 14)
    assert up.starts == 1


def test_the_rejection_reports_the_detectors_score_and_level() -> None:
    script = [True, True, False, False, False, False, False, False]
    turn, _, _, events, _ = build(script, scores=[0.61, 0.74, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    run(turn, len(script))
    rejected = next(e for e in events if e.kind == "onset.rejected")
    assert rejected.detail["max_score"] == pytest.approx(0.74)
    assert rejected.detail["peak_dbfs"] == pytest.approx(-30.0)


def test_a_detector_without_a_level_is_measured_from_the_frame() -> None:
    """EnergyVad reports no level; the frame itself is measured, and silence is None, not -inf."""
    mix = PlaybackMixer(rate=MIC_RATE)
    events: list[TurnEvent] = []
    turn = TurnController(
        mixer=mix,
        vad=EnergyVad(threshold_dbfs=-90.0),
        uplink=RecordingUplink(),
        on_event=events.append,
        idle_onset=(4, 5),
    )
    loud = (np.sin(np.arange(VAD_FRAME) * 0.3) * 3000).astype(np.int16)
    for i, f in enumerate([loud, loud] + [np.zeros(VAD_FRAME, dtype=np.int16)] * 6):
        turn.feed(f, at=i * FRAME_S)
    rejected = next(e for e in events if e.kind == "onset.rejected")
    assert -25 < rejected.detail["peak_dbfs"] < -15


def test_a_rejected_onset_does_not_hold_the_wake_window_open() -> None:
    """A false turn used to keep the desk awake another 20 s, which let in the next one."""
    script = [False] * 10 + [True] * 3 + [False] * 10
    for onset, still_awake in ((IDLE_ONSET, False), (None, True)):
        turn, _, _, _, _ = build(script, onset=onset, wake_window_s=1.0)
        turn.wake(at=0.0)
        run(turn, len(script))
        # The burst was at ~0.35 s; the window opened at 0 closes at 1.0 s.
        assert turn.awake(at=1.1) is still_awake, onset


def test_nothing_is_logged_while_asleep() -> None:
    """Asleep, every breath would be a row in a log kept forever, for nothing."""
    turn, _, up, events, _ = build([True] * 3 + [False] * 8, wake_window_s=20.0)
    run(turn, 11)
    assert up.starts == 0
    assert "onset.rejected" not in kinds(events)


def test_hey_jarvis_whats_the_weather_opens_on_the_frame_after_the_wake() -> None:
    """The window is filled while asleep, so the request is not cut in half by the wake's lag."""
    turn, _, up, events, _ = build([False] * 20 + [True] * 30, wake_window_s=20.0)
    run(turn, 30)
    assert up.starts == 0, "asleep: counted, not acted on"
    turn.wake(at=30 * FRAME_S)
    run(turn, 1, start=30)
    assert up.starts == 1
    start = next(e for e in events if e.kind == "activity_start")
    assert start.detail["preroll_samples"] == round(IDLE_PREROLL_MS / (FRAME_S * 1000)) * VAD_FRAME


# -- the pre-rolls ------------------------------------------------------------


def test_an_idle_commit_reaches_back_640ms() -> None:
    turn, _, up, events, _ = build([False] * 40 + [True] * 4)
    run(turn, 44)
    start = next(e for e in events if e.kind == "activity_start")
    assert start.detail["barge_in"] is False
    assert start.detail["preroll_samples"] == 20 * VAD_FRAME
    assert round(start.detail["preroll_samples"] / MIC_RATE * 1000) == IDLE_PREROLL_MS
    assert up.samples == 20 * VAD_FRAME


def test_a_barge_in_sends_only_416ms_because_older_audio_is_our_own_voice() -> None:
    turn, _, _, events, _ = build([False] * 40 + [True] * 14, speaking=True)
    run(turn, 54)
    start = next(e for e in events if e.kind == "activity_start")
    assert start.detail["barge_in"] is True
    assert start.detail["preroll_samples"] == 13 * VAD_FRAME
    assert round(start.detail["preroll_samples"] / MIC_RATE * 1000) == BARGE_PREROLL_MS


def test_the_defaults_still_send_exactly_320ms_either_way() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    events: list[TurnEvent] = []
    turn = TurnController(
        mixer=mix, vad=ScriptedVad([False] * 40 + [True] * 3), uplink=RecordingUplink(),
        on_event=events.append,
    )  # fmt: skip
    run(turn, 43)
    assert next(e for e in events if e.kind == "activity_start").detail["preroll_samples"] == (
        10 * VAD_FRAME
    )


# -- barge-in keeps its own rule ----------------------------------------------


def test_barge_in_still_ducks_on_three_in_a_row_and_confirms() -> None:
    turn, mix, up, events, _ = build([True] * 14, speaking=True)
    run(turn, 3)
    assert turn.state is TurnState.SUSPECT and mix.ducked and up.starts == 0
    run(turn, 8, start=3)
    assert turn.state is TurnState.USER_SPEAKING and up.starts == 1
    assert "onset.rejected" not in kinds(events)


def test_frames_heard_over_playback_never_count_towards_an_idle_turn() -> None:
    """Two echo frames during playback plus two after it is not four of five."""
    turn, mix, up, _, _ = build([True, True, True, True, False, False], speaking=True)
    run(turn, 2)
    assert turn.state is TurnState.IDLE and not mix.ducked, "two frames do not duck"
    mix.flush()
    assert not mix.is_playing
    run(turn, 4, start=2)
    assert up.starts == 0


def test_an_echo_rejection_starts_the_idle_count_from_nothing() -> None:
    script = [True] * 3 + [False] * 8 + [True] * 3 + [False] * 6
    turn, mix, up, _, _ = build(script, speaking=True)
    run(turn, 11)
    assert turn.echo_rejections == 1
    mix.flush()
    run(turn, 9, start=11)
    assert up.starts == 0


# -- a detector stuck on ------------------------------------------------------


def test_a_turn_stuck_open_is_ended_after_max_turn_s() -> None:
    frames = round(3.0 / FRAME_S)
    turn, _, up, events, vad = build([True], max_turn_s=2.0)
    run(turn, frames)
    assert up.starts == 1 and up.ends == 1, "the guard closed it, so Gemini can answer"
    end = next(e for e in events if e.kind == "activity_end")
    assert end.detail["forced"] == "max_turn" and end.detail["max_turn_s"] == 2.0
    assert 2.0 <= end.at - next(e for e in events if e.kind == "activity_start").at < 2.1
    assert vad.resets == 1, "a fresh detector state, in case the state was what stuck"


def test_after_a_forced_end_the_detector_must_go_quiet_before_another_turn() -> None:
    stuck = round(4.0 / FRAME_S)
    turn, _, up, _, vad = build([True] * stuck + [False] + [True] * 5, max_turn_s=2.0)
    run(turn, stuck)
    assert up.starts == 1, "still on, so no loop of 2-second turns"
    run(turn, 6, start=stuck)
    assert up.starts == 2, "one quiet frame, then a real onset, opens the next"
    assert vad.i == stuck + 6


def test_max_turn_is_off_unless_asked_for() -> None:
    turn, _, up, _, _ = build([True])
    run(turn, round((MAX_TURN_S + 5) / FRAME_S))
    assert up.starts == 1 and up.ends == 0


def test_abort_forgets_a_half_counted_onset() -> None:
    turn, _, up, _, _ = build([True] * 3 + [True])
    run(turn, 3)
    turn.abort("reconnecting", at=1.0)
    run(turn, 1, start=40)
    assert up.starts == 0, "one frame after an abort is one frame, not four"


def test_an_impossible_onset_is_refused() -> None:
    with pytest.raises(ValueError, match="K <= W"):
        build([True], onset=(5, 4))
    with pytest.raises(ValueError):
        build([True], onset=(0, 4))


def test_the_fallbacks_onset_is_quicker_by_one_frame() -> None:
    assert IDLE_ONSET_FALLBACK[0] == IDLE_ONSET[0] - 1
    turn, _, up, _, _ = build([True, True, False, True], onset=IDLE_ONSET_FALLBACK)
    run(turn, 4)
    assert up.starts == 1


# -- the energy gate keeps echo rejection, whatever the model says ------------


class AlwaysSpeech:
    """A Silero session that calls EVERYTHING speech: the worst model there could be."""

    def run(self, _outputs: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        return [np.array([[0.99]], dtype=np.float32), feeds["state"]]


RESIDUAL_ECHO_GAIN = 0.06  # as in test_audio_graph.py: what a desk's AEC is expected to leave


def _graph(echo_gain: float) -> tuple[AudioGraph, SyntheticLeg, PlaybackMixer, RecordingUplink]:
    mix = PlaybackMixer(rate=MIC_RATE)
    up = RecordingUplink()
    turn = TurnController(
        mixer=mix,
        vad=SileroVad(session=AlwaysSpeech(), check=False),
        uplink=up,
        idle_onset=IDLE_ONSET,
        idle_preroll_ms=IDLE_PREROLL_MS,
        preroll_ms=BARGE_PREROLL_MS,
    )
    leg = SyntheticLeg(rate=MIC_RATE, block=320, confirm_ms=200, echo_gain=echo_gain)
    events: list[AudioEvent] = []
    graph = AudioGraph(
        mixer=mix,
        micbus=MicBus(rate=MIC_RATE, seconds=4.0),
        turn=turn,
        aec=leg.make_aec(),
        device_rate=MIC_RATE,
        block=320,
        on_event=events.append,
    )
    return graph, leg, mix, up


def _speech(n: int, amp: int) -> np.ndarray:
    t = np.arange(n)
    wave = np.sin(t * 2 * np.pi * 220 / MIC_RATE) + 0.5 * np.sin(t * 2 * np.pi * 700 / MIC_RATE)
    return (wave / 1.5 * amp).astype(np.int16)


def _blocks(pcm: np.ndarray) -> list[np.ndarray]:
    return [pcm[i : i + 320] for i in range(0, pcm.size - 319, 320)]


def test_a_real_echo_loop_is_still_rejected_when_the_model_hears_speech_everywhere() -> None:
    """Silero is level-blind; the per-frame energy gate is what lets the duck reject echo."""
    graph, leg, mix, up = _graph(RESIDUAL_ECHO_GAIN)
    leg.echo_delay_blocks = 1
    mix.track("live", Prio.LIVE, ttl_s=None).write(_speech(MIC_RATE * 2, 12000), at=0.0)
    leg.feed(_blocks(np.zeros(320 * 40, dtype=np.int16)))
    leg.run(graph)
    assert up.starts == 0, "Jarvis interrupted himself"
    assert graph.turn.echo_rejections >= 1
    assert mix.is_playing


def test_and_a_real_voice_over_that_loop_still_commits() -> None:
    graph, leg, mix, up = _graph(RESIDUAL_ECHO_GAIN)
    mix.track("live", Prio.LIVE, ttl_s=None).write(_speech(MIC_RATE * 2, 8000), at=0.0)
    leg.feed(_blocks(np.zeros(320 * 5, dtype=np.int16)))
    leg.feed(_blocks(_speech(320 * 40, 11000)))
    leg.run(graph)
    assert up.starts == 1 and graph.turn.state is TurnState.USER_SPEAKING
