"""The one output stream: arbitration, the fader, the TTL, and the fidelity refusal.

Two of these tests are not about audio at all. ``test_the_bus_refuses_exact_tier_...``
is the whole system's fidelity guarantee reduced to an exception, and
``test_a_second_output_stream_is_refused`` is rule 2 reduced to one. If either
ever needs a ``# noqa``-shaped workaround, the thing being worked around is the
requirement.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio import BUS_RATE, DEV_RATE, LIVE_TTL_S, MIC_RATE
from jarvis.audio.dsp import MIC_PATH_QUALITY, PLAYBACK_QUALITY
from jarvis.audio.mixer import (
    FidelityViolation,
    PlaybackMixer,
    Prio,
    SecondOutputStream,
    UnknownTrack,
)


def tone(n: int, amp: int = 8000) -> np.ndarray:
    return np.full(n, amp, dtype=np.int16)


def rms(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))


# -- the fidelity guarantee, made structural ----------------------------------


def test_the_bus_refuses_exact_tier_content_on_the_live_track() -> None:
    """Load-bearing text never passes through a generative model. Enforced here.

    Option labels, the ordinal-to-label binding and the read-back are answer keys:
    one wrong word silently builds the wrong thing. The LIVE track is Gemini's
    voice, so routing EXACT audio to it is refused at the door rather than
    discouraged in a docstring.
    """
    mix = PlaybackMixer(rate=BUS_RATE)
    live = mix.track("live", Prio.LIVE)
    with pytest.raises(FidelityViolation) as exc:
        live.write(tone(240), tier="exact")
    assert "EXACT" in str(exc.value)
    assert live.pending == 0, "the refusal must not half-enqueue the audio"


def test_the_verbatim_track_does_carry_exact_tier() -> None:
    mix = PlaybackMixer(rate=BUS_RATE)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    verbatim.write(tone(240), tier="exact")
    assert verbatim.pending == 240


def test_free_and_faithful_tiers_are_fine_on_the_live_track() -> None:
    """Gemini may say "Claude Code has a question about storage" all day."""
    mix = PlaybackMixer(rate=BUS_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(240), tier="free")
    live.write(tone(240), tier="faithful")
    assert live.pending == 480


def test_the_monitor_track_also_refuses_exact_tier() -> None:
    """A -12 dB mix under another voice is not a reading of anything."""
    mix = PlaybackMixer(rate=BUS_RATE)
    monitor = mix.track("monitor", Prio.MONITOR)
    with pytest.raises(FidelityViolation):
        monitor.write(tone(240), tier="exact")


# -- rule 2 -------------------------------------------------------------------


def test_a_second_output_stream_is_refused() -> None:
    mix = PlaybackMixer(rate=BUS_RATE)
    mix.claim_output("desk")
    with pytest.raises(SecondOutputStream) as exc:
        mix.claim_output("some-notification-sound")
    assert "desk" in str(exc.value)
    assert mix.output_owner == "desk"


def test_releasing_the_claim_allows_a_clean_handover() -> None:
    mix = PlaybackMixer(rate=BUS_RATE)
    with mix.claim_output("desk"):
        assert mix.output_owner == "desk"
    assert mix.output_owner is None
    mix.claim_output("phone").release()


def test_pull_returns_the_same_object_it_records_as_the_reference() -> None:
    """Identity, because an equal-but-different array means somebody rebuilt it."""
    mix = PlaybackMixer(rate=BUS_RATE)
    out = mix.pull(480)
    assert out is mix.last_pull()


# -- arbitration --------------------------------------------------------------


def test_verbatim_preempts_live_rather_than_mixing_over_it() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    live.write(tone(320, 6000))
    verbatim.write(tone(320, 6000), tier="exact")

    block = mix.pull(320, at=0.0)
    # Exactly one voice, at its own level — not two summed to 12000.
    assert rms(block) == pytest.approx(6000, abs=1)
    assert live.pending == 320, "Gemini's audio is HELD, not consumed, while the reader speaks"
    assert verbatim.pending == 0


def test_the_monitor_track_is_the_only_one_that_mixes_and_sits_12db_down() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    monitor = mix.track("monitor", Prio.MONITOR)
    live.write(tone(320, 8000))
    monitor.write(tone(320, 8000))
    block = mix.pull(320, at=0.0)
    # 8000 + 8000 * 10**(-12/20) ~= 8000 + 2007
    assert rms(block) == pytest.approx(8000 + 8000 * 10 ** (-12 / 20), rel=0.01)
    assert monitor.pending == 0


def test_preempt_for_fades_the_lower_track_instead_of_clicking() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(1600, 8000))
    dropped = mix.preempt_for(Prio.VERBATIM, fade_ms=10)
    assert dropped == 1600 - 160
    faded = mix.pull(160, at=0.0)
    assert abs(int(faded[0])) > abs(int(faded[-1]))
    assert abs(int(faded[-1])) < 200


def test_system_priority_beats_verbatim() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    system = mix.track("system", Prio.SYSTEM)
    verbatim.write(tone(320, 4000), tier="exact")
    system.write(tone(320, 9000))
    block = mix.pull(320, at=0.0)
    assert rms(block) == pytest.approx(9000, abs=1)


# -- the fader ----------------------------------------------------------------


def test_duck_attenuates_playback_and_touches_nothing_else() -> None:
    """Rule 1: the ONLY thing that changes while Jarvis speaks is playback gain."""
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(3200, 10000))

    before = mix.pull(320, at=0.0)
    mix.duck(-20.0, ramp_ms=20)
    mix.pull(320, at=0.02)  # the ramp block
    ducked = mix.pull(320, at=0.04)

    assert rms(before) == pytest.approx(10000, abs=1)
    assert rms(ducked) == pytest.approx(1000, rel=0.02)
    assert mix.ducked


def test_the_duck_is_interpolated_across_the_block_not_stepped() -> None:
    """A gain step is a click, and a click on the reference is energy the AEC
    cannot model — the duck would cause the false barge-in it exists to reject."""
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(3200, 10000))
    mix.pull(320, at=0.0)
    mix.duck(-20.0, ramp_ms=20)
    ramp = mix.pull(320, at=0.02)
    assert abs(int(ramp[0])) > abs(int(ramp[-1]))
    # Monotonic descent, with no single-sample cliff.
    steps = np.abs(np.diff(ramp.astype(np.int32)))
    assert steps.max() < 200


def test_ramp_back_restores_unity_over_fifty_milliseconds() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(16000, 10000))
    mix.duck(-20.0, ramp_ms=20)
    for i in range(4):
        mix.pull(320, at=i * 0.02)
    mix.ramp_to(1.0, ramp_ms=50)
    for i in range(4, 8):
        mix.pull(320, at=i * 0.02)
    assert mix.fader == pytest.approx(1.0, abs=1e-3)
    assert not mix.ducked


def test_hard_stop_zero_fills_and_stays_zero() -> None:
    """The kill switch's lever. It does not fade; it stops."""
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(3200, 12000))
    mix.hard_stop()
    for i in range(3):
        assert not mix.pull(320, at=i * 0.02).any()
    assert mix.stopped
    mix.resume()
    live.write(tone(320, 12000))
    assert mix.pull(320, at=0.1).any()


# -- staleness ----------------------------------------------------------------


def test_late_live_arrivals_are_dropped_after_the_two_second_ttl() -> None:
    """A barge-in flushes, but Gemini keeps sending for a while. Nothing older
    than the TTL may be played: there is no way to tell the model to stop."""
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    assert live.ttl_s == LIVE_TTL_S
    live.write(tone(320, 9000), at=0.0)
    block = mix.pull(320, at=LIVE_TTL_S + 0.5)
    assert not block.any()
    assert live.dropped_stale == 320


def test_fresh_live_audio_survives_the_ttl_sweep() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(320, 9000), at=1.0)
    assert mix.pull(320, at=1.5).any()
    assert live.dropped_stale == 0


def test_the_verbatim_track_has_no_ttl_because_a_reader_is_never_late() -> None:
    """Option labels synthesised up front and read 4 seconds later are still the
    right words; Gemini's half-finished sentence 4 seconds later is not."""
    mix = PlaybackMixer(rate=MIC_RATE)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    assert verbatim.ttl_s is None
    verbatim.write(tone(320, 9000), tier="exact", at=0.0)
    assert mix.pull(320, at=60.0).any()


# -- housekeeping -------------------------------------------------------------


def test_flush_drops_everything_queued() -> None:
    mix = PlaybackMixer(rate=MIC_RATE)
    live = mix.track("live", Prio.LIVE)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    live.write(tone(640))
    verbatim.write(tone(640), tier="exact")
    assert mix.flush() == 1280
    assert not mix.is_playing


def test_tracks_at_the_bus_rate_contain_no_resampler() -> None:
    """The 24 kHz rate collision is designed out, not managed: Gemini Live,
    Gemini TTS, Kokoro and edge-tts are all natively 24 kHz PCM16 mono."""
    mix = PlaybackMixer(rate=BUS_RATE)
    for name, prio in (("live", Prio.LIVE), ("verbatim", Prio.VERBATIM)):
        assert not mix.track(name, prio, content_rate=BUS_RATE).resamples


def test_an_unknown_track_is_a_lookup_error_not_a_silent_none() -> None:
    mix = PlaybackMixer(rate=BUS_RATE)
    with pytest.raises(UnknownTrack):
        mix.get("nope")
    with pytest.raises(ValueError):
        mix.track("live", Prio.LIVE)
        mix.track("live", Prio.LIVE)


def test_two_mixers_coexist_because_the_phone_is_a_second_process() -> None:
    """No singletons. v1 runs one; the phone leg is a second one, from day one."""
    desk = PlaybackMixer(rate=BUS_RATE)
    phone = PlaybackMixer(rate=MIC_RATE)
    desk.track("live", Prio.LIVE).write(tone(480, 7000))
    desk.claim_output("desk")
    phone.claim_output("phone")  # a different mixer, so no conflict
    assert desk.pull(480, at=0.0).any()
    assert not phone.pull(320, at=0.0).any()


# -- the clock the TTL is measured against ------------------------------------


def test_the_ttl_uses_the_mixers_clock_when_the_caller_does_not_stamp() -> None:
    """The regression the original tests could not see, because they all passed `at`.

    Every real producer calls ``track.write(pcm)`` with no ``at`` — see
    ``jarvis/voice/router.py``. The graph pulls with a clock counted in SAMPLES
    from zero, so a track stamping arrivals from ``time.monotonic()`` computes
    ``now - at`` as a large negative number forever and the 2 s staleness TTL
    silently never fires. Late arrivals from Gemini would then be played two
    seconds after the user interrupted, which is the exact bug the TTL exists
    to prevent.
    """
    ticks = [0.0]
    mix = PlaybackMixer(rate=MIC_RATE, clock=lambda: ticks[0])
    live = mix.track("live", Prio.LIVE)
    verbatim = mix.track("verbatim", Prio.VERBATIM)
    # The reader holds the floor, so the LIVE audio is HELD and must age out.
    verbatim.write(tone(MIC_RATE * 10, 6000), tier="exact")
    live.write(tone(MIC_RATE * 3, 9000))
    assert live.pending > 0

    for i in range(200):  # 4 s of audio at 20 ms a block
        ticks[0] = i * 0.02
        mix.pull(320, at=ticks[0])

    assert live.dropped_stale > 0, "held LIVE audio must age out on the mixer's clock"
    assert live.pending == 0


def test_a_track_stamps_with_the_mixers_clock_not_the_wall_clock() -> None:
    mix = PlaybackMixer(rate=MIC_RATE, clock=lambda: 7.5)
    live = mix.track("live", Prio.LIVE)
    live.write(tone(320, 9000))
    assert live._queue[0].at == 7.5  # noqa: SLF001 - the stamp is the point


def test_playback_resampling_does_not_inherit_the_mic_paths_quality() -> None:
    """MIC_PATH_QUALITY is LQ for a group-delay reason that is about the barge-in
    budget and a 16 kHz uplink. Neither applies to the audio the user hears."""
    mix = PlaybackMixer(rate=DEV_RATE)
    live = mix.track("live", Prio.LIVE, content_rate=BUS_RATE)
    assert live.resamples
    assert live._resampler.quality == PLAYBACK_QUALITY  # noqa: SLF001
    assert PLAYBACK_QUALITY != MIC_PATH_QUALITY
