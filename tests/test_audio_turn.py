"""Duck on suspicion, then confirm — and the veto that stops Jarvis killing himself.

Every test drives the controller with an EXPLICIT ``at``, derived from the frame
index. Nothing sleeps. That is not only faster: a confirm window measured against
a wall clock would pass or fail depending on how busy the CI box was, which is
exactly the flakiness the graph avoids by counting samples instead of seconds.

The three outcomes that matter are one test each:

* a real barge-in COMMITS, losslessly, with the pre-roll;
* an echo is REJECTED, and playback survives it;
* a phone leg SKIPS the confirm entirely, because there is no echo to reject.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio import MIC_RATE, VAD_FRAME
from jarvis.audio.dsp import EnergyVad
from jarvis.audio.mixer import PlaybackMixer, Prio
from jarvis.audio.turn import (
    RecordingUplink,
    TurnController,
    TurnEvent,
    TurnState,
    normalise_phrase,
)

FRAME_S = VAD_FRAME / MIC_RATE  # 32 ms


class ScriptedVad:
    """A VAD whose answers are a list. The chain is the thing under test, not the VAD."""

    def __init__(self, script: list[bool], *, frame_samples: int = VAD_FRAME) -> None:
        self.frame_samples = frame_samples
        self.script = list(script)
        self.i = 0
        self.resets = 0

    def is_speech(self, frame: np.ndarray) -> bool:
        del frame
        answer = self.script[self.i] if self.i < len(self.script) else False
        self.i += 1
        return answer

    def reset(self) -> None:
        self.resets += 1


def frame(amp: int = 0) -> np.ndarray:
    return np.full(VAD_FRAME, amp, dtype=np.int16)


def build(
    script: list[bool] | None = None,
    *,
    speaking: bool = False,
    confirm_ms: int = 200,
    vad: object | None = None,
) -> tuple[TurnController, PlaybackMixer, RecordingUplink, list[TurnEvent]]:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE, ttl_s=None)
    if speaking:
        live.write(np.full(MIC_RATE * 5, 9000, dtype=np.int16))
    events: list[TurnEvent] = []
    up = RecordingUplink()
    turn = TurnController(
        mixer=mix,
        vad=vad if vad is not None else ScriptedVad(script or []),
        uplink=up,
        on_event=events.append,
        confirm_ms=confirm_ms,
    )
    return turn, mix, up, events


def run(turn: TurnController, n: int, *, start: int = 0) -> None:
    for i in range(start, start + n):
        turn.feed(frame(), at=i * FRAME_S)


def kinds(events: list[TurnEvent]) -> list[str]:
    return [e.kind for e in events]


# -- the quiet case -----------------------------------------------------------


def test_three_frames_of_speech_in_a_quiet_room_opens_a_turn() -> None:
    turn, _, up, events = build([True] * 5)
    run(turn, 5)
    assert turn.state is TurnState.USER_SPEAKING
    assert up.starts == 1
    assert "barge_in.suspected" not in kinds(events), "nothing was playing to duck"


def test_two_frames_are_not_enough() -> None:
    turn, _, up, _ = build([True, True, False, True, True, False])
    run(turn, 6)
    assert turn.state is TurnState.IDLE
    assert up.starts == 0


def test_the_preroll_carries_320ms_so_a_commit_loses_no_words() -> None:
    # Ten quiet frames fill the ring, then three of speech commit the turn. The
    # ring is capped at 320 ms, so the commit sends exactly that and not the
    # whole session.
    turn, _, up, events = build([False] * 10 + [True] * 3)
    run(turn, 10)
    assert turn.preroll_samples == 10 * VAD_FRAME
    run(turn, 3, start=10)
    start = next(e for e in events if e.kind == "activity_start")
    assert start.detail["preroll_samples"] == 10 * VAD_FRAME
    assert up.samples == 10 * VAD_FRAME
    assert round(start.detail["preroll_samples"] / MIC_RATE * 1000) == 320


def test_the_preroll_ring_never_grows_past_320ms() -> None:
    turn, _, up, _ = build([False] * 60 + [True] * 3)
    run(turn, 60)
    assert turn.preroll_samples == 10 * VAD_FRAME
    run(turn, 3, start=60)
    assert up.samples == 10 * VAD_FRAME


# -- barge-in: suspicion, then confirmation -----------------------------------


def test_a_real_barge_in_ducks_then_commits_and_flushes_the_mixer() -> None:
    # 3 onset frames, then still firing through the 200 ms confirm window.
    turn, mix, up, events = build([True] * 14, speaking=True)
    assert mix.is_playing

    run(turn, 3)
    assert turn.state is TurnState.SUSPECT
    assert mix.ducked, "duck on suspicion, before anything is committed"
    assert up.starts == 0, "nothing is sent during the confirm window"
    assert "barge_in.suspected" in kinds(events)

    run(turn, 8, start=3)  # past the 200 ms window
    assert turn.state is TurnState.USER_SPEAKING
    assert up.starts == 1
    assert mix.flush() == 0, "the commit already dropped Gemini's queued audio"
    assert not mix.ducked, "and ramped back, because there is nothing left to duck"

    start = next(e for e in events if e.kind == "activity_start")
    assert start.detail["barge_in"] is True
    assert start.detail["dropped_samples"] > 0
    assert turn.barge_ins == 1


def test_a_barge_in_that_turns_out_to_be_echo_is_discarded() -> None:
    """The VAD stops once the echo is 20 dB down. Cost of being wrong: a 200 ms dip."""
    turn, mix, up, events = build([True] * 3 + [False] * 8, speaking=True)
    run(turn, 3)
    assert turn.state is TurnState.SUSPECT
    assert mix.ducked

    # The window opened on frame 2 (t=64 ms) and closes at t=264 ms, which is
    # frame 9 — the first frame stamped at or past it.
    run(turn, 8, start=3)
    assert turn.state is TurnState.IDLE
    assert up.starts == 0, "no turn was opened, so no turn was lost"
    assert not mix.ducked, "playback ramps back over 50 ms"
    assert mix.is_playing, "and Gemini's audio was never flushed"
    assert turn.echo_rejections == 1
    echo = next(e for e in events if e.kind == "barge_in.echo")
    assert echo.detail["window_ms"] >= 200


def test_the_confirm_window_is_decided_on_the_frame_at_its_end() -> None:
    """Residual echo DECAYS. A window that started loud and ended quiet is the
    case being rejected, so a majority vote would get it exactly backwards."""
    # Speech for the onset and most of the window, silent at the decision point.
    turn, mix, up, _ = build([True] * 8 + [False] * 4, speaking=True)
    run(turn, 12)
    assert up.starts == 0
    assert turn.echo_rejections == 1


def test_the_phone_leg_skips_the_confirm_window_entirely() -> None:
    """confirm_ms=0: there is no local echo on a call, so waiting is pure latency."""
    turn, mix, up, events = build([True] * 4, speaking=True, confirm_ms=0)
    run(turn, 3)
    assert turn.state is TurnState.USER_SPEAKING
    assert up.starts == 1
    assert kinds(events).index("barge_in.suspected") < kinds(events).index("activity_start")
    assert turn.barge_ins == 1


def test_the_microphone_is_never_gated_off_while_jarvis_speaks() -> None:
    """Rule 1, stated as a property: audio keeps flowing into the pre-roll the
    whole time Jarvis is talking, which is why a barge-in can be lossless."""
    turn, mix, _, _ = build([False] * 12, speaking=True)
    run(turn, 12)
    assert mix.is_playing
    assert turn.preroll_samples == 10 * VAD_FRAME
    assert turn.state is TurnState.IDLE


# -- closing the turn ---------------------------------------------------------


def test_activity_end_waits_the_full_700ms_hangover() -> None:
    """Not 100 ms: that figure in the Live guide is for SERVER VAD."""
    speech = [True] * 5
    silence_frames = round(700 / (FRAME_S * 1000))  # 22 frames
    turn, _, up, events = build(speech + [False] * (silence_frames + 2))
    run(turn, 5)
    assert turn.state is TurnState.USER_SPEAKING

    run(turn, silence_frames - 1, start=5)
    assert up.ends == 0, "700 ms has not elapsed yet"

    run(turn, 2, start=5 + silence_frames - 1)
    assert up.ends == 1
    assert turn.state is TurnState.IDLE
    end = next(e for e in events if e.kind == "activity_end")
    assert end.detail["hangover_ms"] == 700


def test_audio_keeps_streaming_through_a_pause_shorter_than_the_hangover() -> None:
    turn, _, up, _ = build([False] * 10 + [True] * 3 + [False] * 5 + [True] * 5)
    run(turn, 23)
    assert turn.state is TurnState.USER_SPEAKING
    assert up.ends == 0
    # 320 ms of pre-roll (the ring is capped), then the ten frames after the
    # commit including the 160 ms pause: a gap in the middle of a sentence is
    # not the end of a turn.
    assert up.samples == (10 + 10) * VAD_FRAME


def test_a_session_that_drops_mid_turn_closes_the_turn_and_resets() -> None:
    turn, _, up, events = build([True] * 4)
    run(turn, 4)
    assert turn.state is TurnState.USER_SPEAKING

    turn.abort("live session closed: GoAway", at=10.0)
    assert turn.state is TurnState.IDLE
    assert up.ends == 1
    aborted = next(e for e in events if e.kind == "turn.aborted")
    assert aborted.detail["was"] == "user_speaking"
    assert turn.preroll_samples == 0


def test_abort_survives_an_uplink_that_is_already_dead() -> None:
    """The socket is gone; the state machine must still reset or no turn ever opens again."""

    class DeadUplink:
        def activity_start(self) -> None: ...

        def send(self, pcm: np.ndarray) -> None: ...

        def activity_end(self) -> None:
            raise ConnectionResetError("the session is gone")

    mix = PlaybackMixer(rate=MIC_RATE)
    events: list[TurnEvent] = []
    turn = TurnController(
        mixer=mix,
        vad=ScriptedVad([True] * 4),
        uplink=DeadUplink(),
        on_event=events.append,
    )
    run(turn, 4)
    turn.abort("socket died", at=1.0)
    assert turn.state is TurnState.IDLE
    assert "turn.end_failed" in kinds(events)


def test_abort_during_the_confirm_window_restores_playback() -> None:
    turn, mix, _, _ = build([True] * 3, speaking=True)
    run(turn, 3)
    assert mix.ducked
    turn.abort("reconnecting", at=1.0)
    assert not mix.ducked


# -- the self-speech veto -----------------------------------------------------


def test_reading_an_issue_containing_the_kill_phrase_does_not_halt_the_system() -> None:
    """The issue that contains the kill phrase will be the one about the kill switch."""
    turn, *_ = build([])
    turn.note_output_transcript(
        "Issue 41 says: the kill switch should respond to jarvis full stop within one second.",
        at=100.0,
    )
    assert turn.detector_hit("jarvis full stop", at=100.5) is False
    assert turn.vetoed("Jarvis, full stop!", at=100.5), "punctuation and case must not evade it"


def test_the_veto_expires_with_the_nine_second_window() -> None:
    turn, *_ = build([])
    turn.note_output_transcript("jarvis full stop", at=100.0)
    assert turn.detector_hit("jarvis full stop", at=105.0) is False
    assert turn.detector_hit("jarvis full stop", at=115.0) is True


def test_a_long_read_aloud_does_not_expire_its_own_opening_line() -> None:
    """A 40-second issue body must veto its first sentence too, not just the last nine seconds."""
    turn, *_ = build([])
    turn.note_output_transcript("jarvis full stop is the phrase", at=100.0)
    for t in range(101, 140):
        turn.note_output_transcript(f"continuing to read line {t}", at=float(t))
    assert turn.detector_hit("jarvis full stop", at=139.5) is False


def test_a_genuine_command_is_not_vetoed() -> None:
    turn, *_ = build([])
    turn.note_output_transcript("Here is the third item on your list.", at=100.0)
    assert turn.detector_hit("jarvis full stop", at=100.5) is True


def test_nav_and_wake_go_through_the_same_veto() -> None:
    """One graph, three requirements: the kill switch, briefing nav and the wake word."""
    turn, _, _, events = build([])
    turn.note_output_transcript(
        "...and then say next to move on, or hey jarvis to wake me.", at=50.0
    )
    assert turn.detector_hit("next", at=50.2) is False
    assert turn.detector_hit("hey jarvis", at=50.2) is False
    assert turn.detector_hit("skip to inbox", at=50.2) is True
    assert kinds(events).count("detector.vetoed") == 2


def test_normalise_phrase_casefolds_turkish_safely() -> None:
    """ "I".lower() is not "ı"; a dotted-I mismatch would silently break the veto."""
    assert normalise_phrase("  Hey,  JARVIS! ") == "hey jarvis"
    assert normalise_phrase("İPTAL") == normalise_phrase("i̇ptal")


# -- plumbing -----------------------------------------------------------------


def test_a_wrong_sized_frame_is_refused_rather_than_padded() -> None:
    turn, *_ = build([])
    with pytest.raises(ValueError):
        turn.feed(np.zeros(256, dtype=np.int16), at=0.0)


def test_the_energy_vad_drives_the_same_chain() -> None:
    """The default VAD, on synthetic audio, end to end — no scripting."""
    mix = PlaybackMixer(rate=MIC_RATE)
    up = RecordingUplink()
    turn = TurnController(mixer=mix, vad=EnergyVad(frame_samples=VAD_FRAME), uplink=up)
    quiet = np.zeros(VAD_FRAME, dtype=np.int16)
    loud = (np.sin(np.arange(VAD_FRAME) * 0.3) * 6000).astype(np.int16)
    for i in range(4):
        turn.feed(quiet, at=i * FRAME_S)
    assert turn.state is TurnState.IDLE
    for i in range(4, 8):
        turn.feed(loud, at=i * FRAME_S)
    assert turn.state is TurnState.USER_SPEAKING
    assert up.starts == 1


def test_two_controllers_run_independently() -> None:
    """Multi-instantiable from the first commit, even though v1 runs one."""
    a, _, up_a, _ = build([True] * 4)
    b, _, up_b, _ = build([False] * 4)
    run(a, 4)
    run(b, 4)
    assert (up_a.starts, up_b.starts) == (1, 0)
    assert (a.state, b.state) == (TurnState.USER_SPEAKING, TurnState.IDLE)
