"""The voice detectors: Silero on onnxruntime, and the numpy fallback.

Two halves. The first runs everywhere, with a fake onnxruntime session, and
pins the wrapper's contract with the model: 64 samples of context in front of
every frame, the state carried from call to call, the hysteresis, and the
per-frame energy gate that keeps echo rejection intact. The second runs the
real model when it is on disk (the Windows workflow fetches it before the
tests; locally ``python -m jarvis.audio.vadmodel download packaging/models``)
and proves the thing the fake cannot: without the context the model is deaf,
with it a vowel is speech and a breath is not.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

from jarvis.audio import BARGE_PREROLL_MS, IDLE_ONSET, IDLE_PREROLL_MS, MIC_RATE, VAD_FRAME
from jarvis.audio import vadmodel as vm
from jarvis.audio.dsp import (
    EnergyVad,
    Hearing,
    SileroVad,
    VadUnavailable,
    VoicedEnergyVad,
    dbfs,
    synthetic_breath,
    synthetic_vowel,
    voicing,
)
from jarvis.audio.mixer import PlaybackMixer, Prio
from jarvis.audio.turn import RecordingUplink, TurnController

FRAME_S = VAD_FRAME / MIC_RATE


class FakeSession:
    """Records what the wrapper hands onnxruntime; answers from ``probs`` or a function."""

    def __init__(self, probs: list[float] | None = None, fn=None) -> None:
        self.probs = list(probs or [])
        self.fn = fn
        self.calls: list[dict[str, np.ndarray]] = []

    def run(self, outputs: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        assert outputs is None
        self.calls.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        if self.fn is not None:
            p = self.fn(feeds["input"])
        else:
            p = self.probs[len(self.calls) - 1] if len(self.calls) <= len(self.probs) else 0.0
        return [np.array([[p]], dtype=np.float32), feeds["state"] + 1.0]


def loud(seed: int = 0, level: float = -25.0) -> np.ndarray:
    x = np.random.default_rng(seed).standard_normal(VAD_FRAME)
    x = x / np.sqrt(np.mean(x**2)) * 32768 * 10 ** (level / 20)
    return x.astype(np.int16)


def frames(pcm: np.ndarray) -> np.ndarray:
    return pcm[: pcm.size // VAD_FRAME * VAD_FRAME].reshape(-1, VAD_FRAME)


# ───────────────────────────── the wrapper's contract ─────────────────────────────


def test_every_frame_goes_in_behind_the_last_64_samples_of_the_one_before() -> None:
    """Without these 64 samples Silero v5+ runs, raises nothing, and never says speech."""
    session = FakeSession([0.1, 0.1, 0.1])
    vad = SileroVad(session=session, check=False)
    a, b = loud(1), loud(2)
    vad.is_speech(a)
    vad.is_speech(b)
    first, second = (c["input"] for c in session.calls[:2])
    assert first.shape == second.shape == (1, 64 + 512)
    assert first.dtype == np.float32
    np.testing.assert_array_equal(first[0, :64], np.zeros(64, dtype=np.float32))
    np.testing.assert_allclose(first[0, 64:], a / 32768.0, rtol=0, atol=1e-7)
    np.testing.assert_allclose(second[0, :64], a[-64:] / 32768.0, rtol=0, atol=1e-7)
    np.testing.assert_allclose(second[0, 64:], b / 32768.0, rtol=0, atol=1e-7)


def test_the_state_is_carried_and_the_rate_is_16k() -> None:
    session = FakeSession([0.1] * 3)
    vad = SileroVad(session=session, check=False)
    for i in range(3):
        vad.is_speech(loud(i))
    states = [c["state"] for c in session.calls]
    assert states[0].shape == (2, 1, 128) and not states[0].any()
    assert (states[1] == 1.0).all() and (states[2] == 2.0).all(), "stateN feeds the next call"
    assert all(c["sr"].dtype == np.int64 and int(c["sr"]) == 16_000 for c in session.calls)


def test_reset_starts_a_cold_stream() -> None:
    session = FakeSession([0.9] * 4)
    vad = SileroVad(session=session, check=False)
    vad.is_speech(loud(1))
    vad.reset()
    vad.is_speech(loud(2))
    assert not session.calls[1]["state"].any()
    assert not session.calls[1]["input"][0, :64].any()


def test_enter_at_half_and_stay_until_under_0_35() -> None:
    vad = SileroVad(session=FakeSession([0.4, 0.6, 0.4, 0.36, 0.34, 0.45, 0.5]), check=False)
    assert [vad.is_speech(loud(i)) for i in range(7)] == [
        False, True, True, True, False, False, True,
    ]  # fmt: skip


def test_a_quiet_frame_is_never_speech_whatever_the_model_says() -> None:
    """Silero is level-blind ("stop" scored 0.96 at -46 dBFS); ducked echo is QUIET."""
    vad = SileroVad(session=FakeSession([0.99, 0.99, 0.99]), check=False)
    assert vad.is_speech(loud(1, -25.0)) is True
    assert vad.is_speech(loud(2, -50.0)) is False
    assert vad.last_score == pytest.approx(0.99) and vad.last_dbfs < -45


def test_silero_speech_implies_energy_speech_frame_by_frame() -> None:
    """The invariant that makes swapping the VAD unable to weaken echo rejection."""
    rng = np.random.default_rng(3)
    vad = SileroVad(session=FakeSession(list(rng.uniform(0, 1, 400))), check=False)
    energy = EnergyVad(threshold_dbfs=-45.0)
    for i in range(400):
        frame = loud(i, float(rng.uniform(-70, -10)))
        if vad.is_speech(frame):
            assert energy.is_speech(frame), f"frame {i} at {dbfs(frame):.1f} dBFS"


def test_a_model_that_cannot_hear_a_vowel_is_refused_at_construction() -> None:
    with pytest.raises(VadUnavailable, match="cannot tell speech from a breath"):
        SileroVad(session=FakeSession(fn=lambda x: 0.001))
    with pytest.raises(VadUnavailable, match="cannot tell"):
        SileroVad(session=FakeSession(fn=lambda x: 1.0))  # calls everything speech


def test_the_self_check_puts_the_stream_back_as_it_was() -> None:
    session = FakeSession(fn=lambda x: 0.9)
    vad = SileroVad(session=session, check=False)
    vad.is_speech(loud(1))
    before = (vad._state.copy(), vad._context.copy())
    vad.self_check()
    np.testing.assert_array_equal(vad._state, before[0])
    np.testing.assert_array_equal(vad._context, before[1])


def test_wrong_sizes_are_refused() -> None:
    vad = SileroVad(session=FakeSession(), check=False)
    with pytest.raises(ValueError):
        vad.is_speech(np.zeros(480, dtype=np.int16))
    with pytest.raises(ValueError, match="512"):
        SileroVad(session=FakeSession(), frame_samples=256, check=False)


def test_no_path_and_no_runtime_are_vad_unavailable(tmp_path: Path) -> None:
    with pytest.raises(VadUnavailable, match="no Silero model path"):
        SileroVad()
    garbage = tmp_path / "silero_vad.onnx"
    garbage.write_bytes(b"not a model")
    pytest.importorskip("onnxruntime")
    with pytest.raises(VadUnavailable, match="could not load"):
        SileroVad(garbage)


def test_hearing_is_ok_only_when_both_sides_are_right() -> None:
    assert Hearing(vowel=0.98, breath=0.11, threshold=0.5).ok
    assert not Hearing(vowel=0.001, breath=0.0, threshold=0.5).ok
    assert not Hearing(vowel=0.99, breath=0.7, threshold=0.5).ok
    assert "0.98" in Hearing(vowel=0.98, breath=0.11, threshold=0.5).describe()


def test_the_synthetic_sounds_are_what_they_claim() -> None:
    v, b = synthetic_vowel(), synthetic_breath()
    assert v.dtype == b.dtype == np.int16
    assert dbfs(v) == pytest.approx(-25.0, abs=0.5) and dbfs(b) == pytest.approx(-25.0, abs=0.5)
    lags = np.arange(40, 321)
    middle = frames(v)[10]
    assert voicing(middle, lags) > 0.8, "a vowel is periodic"
    assert voicing(frames(b)[10], lags) < 0.5, "a breath is not"
    np.testing.assert_array_equal(synthetic_breath(), b)  # seeded: the check is repeatable


# ───────────────────────────── the fallback ─────────────────────────────


def _naive_voicing(frame: np.ndarray, lags: np.ndarray) -> float:
    x = frame.astype(np.float64)
    x -= x.mean()
    e = float(np.dot(x, x))
    if e <= 0:
        return 0.0
    ac = np.correlate(x, x, "full")[x.size - 1 :]
    norm = np.sqrt(e * np.array([np.dot(x[lag:], x[lag:]) for lag in lags]) + 1e-9)
    return float(np.max(ac[lags] / norm))


@pytest.mark.parametrize("seed", range(6))
def test_the_fft_voicing_is_the_measured_one(seed: int) -> None:
    """The 14/14 and 0/13 numbers were measured with the slow loop; the FFT must agree."""
    lags = np.arange(40, 321)
    rng = np.random.default_rng(seed)
    for frame in (loud(seed), frames(synthetic_vowel(f0=float(rng.uniform(80, 300))))[8]):
        assert voicing(frame, lags) == pytest.approx(_naive_voicing(frame, lags), abs=1e-9)
    assert voicing(np.zeros(VAD_FRAME, dtype=np.int16), lags) == 0.0


def test_the_fallback_hears_a_vowel_and_not_a_breath_as_loud() -> None:
    vad = VoicedEnergyVad()
    pad = np.zeros(4096, dtype=np.int16)
    said = [vad.is_speech(f) for f in frames(np.concatenate((pad, synthetic_vowel(), pad)))]
    assert sum(said) >= 15
    vad.reset()
    breath = np.concatenate((pad, synthetic_breath(level_dbfs=-24.0), pad))
    assert not any(vad.is_speech(f) for f in frames(breath))


def test_the_fallback_holds_voicing_through_a_consonant() -> None:
    """ "stop": the "st" is loud and unvoiced, and must not cut the word."""
    vad = VoicedEnergyVad(hold_frames=6)
    for f in frames(synthetic_vowel(0.3)):
        vad.is_speech(f)
    hiss = loud(9, -30.0)
    assert vad.is_speech(hiss) is True
    vad.reset()
    assert vad.is_speech(hiss) is False


def test_the_fallback_is_quiet_below_the_energy_gate() -> None:
    vad = VoicedEnergyVad()
    quiet = synthetic_vowel(level_dbfs=-60.0)
    assert not any(vad.is_speech(f) for f in frames(quiet))


# ───────────────────────────── the real model ─────────────────────────────


def _real_model_path() -> Path:
    pytest.importorskip("onnxruntime")
    path = vm.find()
    if path is None:
        if sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("the workflow's 'Fetch the voice activity model' step left no model")
        pytest.skip(
            "no silero_vad.onnx (python -m jarvis.audio.vadmodel download packaging/models)"
        )
    return path


def test_the_real_model_hears_a_vowel_and_ignores_a_breath() -> None:
    vad = SileroVad(_real_model_path())
    assert vad.hearing is not None and vad.hearing.ok
    assert vad.hearing.vowel > 0.8 and vad.hearing.breath < 0.3


def test_without_the_context_the_real_model_is_deaf() -> None:
    """The bug this wrapper exists to make impossible, shown on the real model."""
    path = _real_model_path()
    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    pad = np.zeros(4096, dtype=np.int16)
    clip = frames(np.concatenate((pad, synthetic_vowel(), pad)))
    state = np.zeros((2, 1, 128), dtype=np.float32)
    bare = []
    for f in clip:
        out, state = session.run(
            None,
            {
                "input": (f.astype(np.float32) / 32768.0)[None, :],
                "state": state,
                "sr": np.array(16_000, dtype=np.int64),
            },
        )
        bare.append(float(out[0, 0]))
    with_context = SileroVad(path, check=False)
    heard = [with_context.probability(f) for f in clip]
    assert max(bare) < 0.05, "a bare 512-sample frame is deaf"
    assert max(heard) > 0.8, "the same audio with its 64 samples of context is speech"


def test_a_tampered_model_is_refused(tmp_path: Path) -> None:
    data = bytearray(_real_model_path().read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = tmp_path / vm.MODEL
    bad.write_bytes(bytes(data))
    assert not vm.verified(bad)
    with pytest.raises(vm.VadModelMissing, match="not the pinned"):
        vm.load(bad)


def _say(text: str) -> np.ndarray:
    """The OS voice: espeak-ng on Linux, SAPI on Windows. 24 kHz in, 16 kHz out."""
    from jarvis.voice.engines import EngineFailed, EngineUnavailable, SystemEngine

    try:
        pcm = SystemEngine().synth(text, "en")
    except (EngineUnavailable, EngineFailed) as exc:
        pytest.skip(f"no OS voice: {exc}")
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(audio.size * MIC_RATE / 24_000)
    out = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio)
    return out.astype(np.int16)


def _desk_turn(vad: SileroVad, *, speaking: bool = False) -> tuple[TurnController, RecordingUplink]:
    mix = PlaybackMixer(rate=MIC_RATE)
    if speaking:
        mix.track("live", Prio.LIVE, ttl_s=None).write(np.full(MIC_RATE * 6, 6000, np.int16))
    up = RecordingUplink()
    turn = TurnController(
        mixer=mix,
        vad=vad,
        uplink=up,
        idle_onset=IDLE_ONSET,
        idle_preroll_ms=IDLE_PREROLL_MS,
        preroll_ms=BARGE_PREROLL_MS,
    )
    return turn, up


def _feed(turn: TurnController, pcm: np.ndarray) -> None:
    for i, f in enumerate(frames(pcm)):
        turn.feed(f, at=i * FRAME_S)


@pytest.mark.parametrize("text", ["open notepad", "yes", "stop"])
def test_a_spoken_word_opens_a_turn_through_the_real_model(text: str) -> None:
    turn, up = _desk_turn(SileroVad(_real_model_path()))
    word = _say(text)
    room = np.zeros(MIC_RATE // 2, dtype=np.int16)
    level = 10 ** ((-28 - dbfs(word)) / 20)
    _feed(turn, np.concatenate((room, (word * level).astype(np.int16), room)))
    assert up.starts == 1, f"{text!r} did not open a turn"


@pytest.mark.parametrize("level", [-36.0, -26.0])
def test_a_breath_as_loud_as_speech_opens_nothing_through_the_real_model(level: float) -> None:
    room = np.zeros(MIC_RATE // 2, dtype=np.int16)
    for speaking in (False, True):
        turn, up = _desk_turn(SileroVad(_real_model_path()), speaking=speaking)
        _feed(turn, np.concatenate((room, synthetic_breath(level_dbfs=level), room)))
        assert up.starts == 0 and turn.barge_ins == 0, (level, speaking)


def test_the_real_model_costs_well_under_a_callback() -> None:
    import time

    vad = SileroVad(_real_model_path())
    f = loud(1)
    start = time.perf_counter()
    for _ in range(200):
        vad.is_speech(f)
    per_frame_ms = (time.perf_counter() - start) * 1000 / 200
    # A 20 ms callback runs ~0.6 VAD frames; measured ~0.2 ms. Generous for CI.
    assert per_frame_ms < 5.0, per_frame_ms
