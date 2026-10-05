"""DSP as protocols with honest null defaults, so the graph runs with nothing installed.

THREE OBVIOUS AEC CHOICES ARE DEAD and one never existed: ``pyaudio-webrtc-apm``
is not on PyPI at all, ``speexdsp`` last shipped in 2018 (sdist only, and Speex
MDF is a generation behind AEC3), ``webrtc-audio-processing`` ships armv7l wheels
only, and ``aec-audio-processing`` is Windows-only. The live choice is
``pywebrtc-audio`` 0.2.0 — WebRTC AEC3, the algorithm in Chrome, C++ with the GIL
released so it can run inside the PortAudio callback — with LiveKit's
``rtc.AudioProcessingModule`` as a drop-in second source.

None of that is installed in CI and none of it is importable on a box with no
sound card, which is exactly why every stage here is a Protocol whose default
implementation is a passthrough. AEC is not a dependency of the graph; it is a
quality of the desk leg. The phone leg deliberately has none at all — the carrier
and handset already cancel, and a second AEC on a line that already has one makes
things worse.

THE PASSTHROUGH IS NOT A STUB TO BE REPLACED LATER. It is the phone leg's real
implementation and it is the desk leg's documented degraded mode. So it reports
``erle_db = 0.0`` rather than pretending, and :func:`erle_db` measures what the
canceller actually achieved rather than what it claims.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from jarvis.audio import SILERO_ENTER, SILERO_EXIT, VAD_MIN_DBFS

__all__ = [
    "AecUnavailable",
    "EchoCanceller",
    "EnergyVad",
    "Hearing",
    "NoiseSuppressor",
    "NullEchoCanceller",
    "MIC_PATH_QUALITY",
    "NullNoiseSuppressor",
    "NullResampler",
    "PLAYBACK_QUALITY",
    "Resampler",
    "ResamplerUnavailable",
    "SileroVad",
    "SoxrResampler",
    "Vad",
    "VadUnavailable",
    "VoicedEnergyVad",
    "WebRtcEchoCanceller",
    "dbfs",
    "erle_db",
    "make_resampler",
    "synthetic_breath",
    "synthetic_vowel",
    "voicing",
]

_INT16_FULL_SCALE = 32768.0


class AecUnavailable(RuntimeError):
    """No echo canceller could be loaded. The caller decides whether that is fatal."""


class VadUnavailable(RuntimeError):
    """No neural VAD could be loaded; the energy VAD is always available."""


class ResamplerUnavailable(RuntimeError):
    """A rate conversion was asked for and soxr is not installed."""


def dbfs(pcm: np.ndarray) -> float:
    """RMS level of an int16 block in dBFS. Silence is -inf, not an exception."""
    if pcm.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))
    if rms <= 0.0:
        return -math.inf
    return 20.0 * math.log10(rms / _INT16_FULL_SCALE)


def erle_db(near: np.ndarray, clean: np.ndarray) -> float:
    """Echo return loss enhancement: how much of the near signal the AEC removed.

    Deliberately NOT averaged over the whole recording. The bench feeds it only
    frames where the far end was actually loud, because ERLE over silence is a
    ratio of two noise floors and will happily report 30 dB from a canceller that
    does nothing at all.
    """
    if near.size == 0 or clean.size == 0:
        return 0.0
    num = float(np.mean(np.square(near.astype(np.float64))))
    den = float(np.mean(np.square(clean.astype(np.float64))))
    if num <= 0.0 or den <= 0.0:
        return 0.0
    return 10.0 * math.log10(num / den)


@runtime_checkable
class EchoCanceller(Protocol):
    """Stage A of the APM, at the device rate, inside the callback.

    ``far`` MUST be the literal post-fader post-mix array the mixer handed to the
    device. Anything else — a copy taken before the fade, a second mix, another
    process's audio — is echo this cannot see and the user will hear.
    """

    sample_rate: int
    stream_delay_ms: int

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray: ...

    @property
    def erle_db(self) -> float: ...


class NullEchoCanceller:
    """No cancellation. The phone leg's real implementation, and the desk's fallback."""

    def __init__(self, sample_rate: int, *, stream_delay_ms: int = 0) -> None:
        self.sample_rate = sample_rate
        self.stream_delay_ms = stream_delay_ms
        self._erle = 0.0

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
        del far
        return near

    @property
    def erle_db(self) -> float:
        # Reported rather than omitted: a TUI showing 0 dB is telling the truth,
        # and a TUI showing nothing is indistinguishable from a broken metric.
        return self._erle


class WebRtcEchoCanceller:
    """AEC3 via ``pywebrtc-audio``, imported lazily so this module stays importable.

    The delay hint is free in a duplex callback:
    ``(tinfo.outputBufferDacTime - tinfo.inputBufferAdcTime) * 1000``. AEC3 has
    its own estimator, so the hint only buys convergence in ~0.5 s instead of
    ~3 s; re-set it only when it moves by more than 5 ms, because thrashing the
    filter is worse than a slightly stale hint.
    """

    DELAY_RESET_THRESHOLD_MS = 5

    def __init__(self, sample_rate: int, *, channels: int = 1, stream_delay_ms: int = 0) -> None:
        try:
            import pywebrtc_audio
        except Exception as exc:  # noqa: BLE001 - any import failure is the same answer
            raise AecUnavailable(
                "pywebrtc-audio is not installed; install the 'aec' extra or run "
                "with NullEchoCanceller and accept duck-confirm barge-in only"
            ) from exc
        self.sample_rate = sample_rate
        self._impl = pywebrtc_audio.EchoCanceller(sample_rate, channels, stream_delay_ms)
        self._delay = stream_delay_ms
        self._erle = 0.0

    @property
    def stream_delay_ms(self) -> int:
        return self._delay

    @stream_delay_ms.setter
    def stream_delay_ms(self, value: int) -> None:
        if abs(value - self._delay) < self.DELAY_RESET_THRESHOLD_MS:
            return
        self._delay = value
        self._impl.stream_delay_ms = value

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
        clean = np.asarray(self._impl.process(near, far), dtype=np.int16)
        self._erle = erle_db(near, clean)
        return clean

    @property
    def erle_db(self) -> float:
        return self._erle


@runtime_checkable
class NoiseSuppressor(Protocol):
    """Stage B of the APM, in the uplink worker at 16 kHz.

    Deliberately separate from the canceller: AEC+NS+AGC as one chain is tuned
    for a human listener and distorts speech in ways that hurt keyword spotting,
    and AGC pumping on an open desk mic causes more trouble than it fixes. Desk
    is NS level 1 with AGC off; phone is NS level 2 with AGC on, because PSTN
    levels vary by 30 dB.
    """

    level: int

    def process(self, pcm: np.ndarray) -> np.ndarray: ...


class NullNoiseSuppressor:
    def __init__(self, level: int = 0) -> None:
        self.level = level

    def process(self, pcm: np.ndarray) -> np.ndarray:
        return pcm


@runtime_checkable
class Vad(Protocol):
    """Speech / not speech on one fixed-size frame.

    Frame size is part of the protocol because the onset count is expressed in
    frames: three 32 ms frames is ~96 ms of evidence, and a VAD with a different
    frame silently retunes the whole barge-in budget.
    """

    frame_samples: int

    def is_speech(self, frame: np.ndarray) -> bool: ...

    def reset(self) -> None: ...


class EnergyVad:
    """A threshold on RMS level. The default, and the reason CI can test barge-in.

    Not a good VAD. It is a HONEST one: it fires on any loud thing, which for the
    duck-confirm chain is the conservative direction — a false onset costs a
    barely audible 200 ms dip and is then rejected by the confirm window. Silero
    goes behind this same protocol without changing a line above it.
    """

    def __init__(self, *, frame_samples: int = 512, threshold_dbfs: float = -45.0) -> None:
        self.frame_samples = frame_samples
        self.threshold_dbfs = threshold_dbfs

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.shape[0] != self.frame_samples:
            raise ValueError(
                f"VAD frame must be {self.frame_samples} samples, got {frame.shape[0]}"
            )
        return dbfs(frame) > self.threshold_dbfs

    def reset(self) -> None:
        # Stateless by construction, so an aborted turn cannot leave it hot.
        return None


@dataclass(frozen=True)
class Hearing:
    """What a VAD made of two sounds it must tell apart: a vowel and a breath.

    The vowel is the deaf test. A Silero wrapper that drops the 64-sample
    context runs without an error and scores this vowel 0.001 instead of 0.98,
    so "the model loaded" proves nothing; "the model heard a vowel" does. The
    breath is the opposite failure, a model that calls everything speech.
    """

    vowel: float
    breath: float
    threshold: float

    @property
    def ok(self) -> bool:
        return self.vowel >= self.threshold > self.breath

    def describe(self) -> str:
        return f"hears a vowel ({self.vowel:.2f}), ignores a breath ({self.breath:.2f})"


def synthetic_vowel(
    seconds: float = 0.8, *, rate: int = 16_000, f0: float = 120.0, level_dbfs: float = -25.0
) -> np.ndarray:
    """An /a/: harmonics of a wobbling 120 Hz pitch shaped by /a/'s three formants.

    Built additively rather than by filtering a pulse train because an IIR
    filter is a Python loop here (no scipy in the bundle), and this runs inside
    the desk's startup and the exe's selftest.
    """
    n = int(rate * seconds)
    t = np.arange(n) / rate
    pitch = f0 * (1 + 0.03 * np.sin(2 * np.pi * 4.5 * t))
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    k = np.arange(1, int(4000 / f0))
    shape = sum(
        1 / (1 + ((k * f0 - f) / bw) ** 2) for f, bw in ((700, 110), (1220, 120), (2600, 160))
    )
    x = ((shape / k)[:, None] * np.sin(k[:, None] * phase[None, :])).sum(axis=0)
    return _at_level(x * _fade(n, int(0.05 * rate), raised=False), level_dbfs)


def synthetic_breath(
    seconds: float = 0.7, *, rate: int = 16_000, level_dbfs: float = -25.0, seed: int = 1
) -> np.ndarray:
    """An inhale: 500 Hz-6 kHz noise, swelling and fading. Loud on purpose: as loud as speech."""
    n = int(rate * seconds)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / rate)
    spectrum[(freqs < 500) | (freqs > 6000)] = 0
    x = np.fft.irfft(spectrum, n)
    return _at_level(x * _fade(n, int(n * 0.3), raised=True), level_dbfs)


def _fade(n: int, edge: int, *, raised: bool) -> np.ndarray:
    env = np.ones(n)
    ramp = np.sin(np.linspace(0, np.pi / 2, edge)) ** 2 if raised else np.linspace(0, 1, edge)
    env[:edge] = ramp
    env[n - edge :] = ramp[::-1]
    return env


def _at_level(x: np.ndarray, level_dbfs: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(np.square(x)))) or 1.0
    y = x / rms * _INT16_FULL_SCALE * 10 ** (level_dbfs / 20)
    return np.clip(y, -32768, 32767).astype(np.int16)


class SileroVad:
    """Silero VAD v5+ on onnxruntime, called directly, AND-ed with the energy gate.

    WHY NOT A PACKAGE. ``pip install silero-vad`` drags torch in even for the
    ONNX path, and sherpa-onnx (what this used to need) is in no extra and not
    in the exe, so the old wrapper could only ever raise. onnxruntime is
    already shipped for the wake word, and one model is one ``InferenceSession``.

    THE CONTEXT IS NOT OPTIONAL. Each 512-sample frame goes in behind the last
    64 samples of the previous input, 576 in all. Without them the model runs,
    raises nothing, and never says speech again, which is why the constructor
    refuses a model that cannot hear :func:`synthetic_vowel`.

    THE ENERGY GATE IS PER FRAME. Silero is nearly level-invariant ("stop"
    scored 0.96 at -46 dBFS), and the duck-confirm chain rejects echo by its
    being 20 dB down. So a frame is speech only when Silero says so AND it is
    above ``min_dbfs``, which makes "Silero says speech" imply "EnergyVad says
    speech" frame by frame: echo rejection can only get better.
    """

    #: Fixed by the model since v5.0, not a knob: any wrapper written for v4,
    #: which had no context, feeds a v5+ model bare frames and is deaf.
    CONTEXT = 64

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        session: Any = None,
        frame_samples: int = 512,
        threshold: float = SILERO_ENTER,
        exit_threshold: float = SILERO_EXIT,
        min_dbfs: float = VAD_MIN_DBFS,
        check: bool = True,
    ) -> None:
        if frame_samples != 512:
            # The 16 kHz model takes exactly 512 new samples per call; other
            # sizes run and score nonsense.
            raise ValueError(f"Silero at 16 kHz takes 512-sample frames, not {frame_samples}")
        self._session = session if session is not None else _silero_session(model_path)
        self.frame_samples = frame_samples
        self.threshold = threshold
        self.exit_threshold = exit_threshold
        self.min_dbfs = min_dbfs
        self._sr = np.array(16_000, dtype=np.int64)
        self.reset()
        #: What the construction-time check measured; None when it was skipped.
        self.hearing: Hearing | None = None
        if check:
            # Also the warm-up: the first run of a session allocates, and it
            # must not land in the audio callback.
            heard = self.hearing = self.self_check()
            if not heard.ok:
                raise VadUnavailable(
                    f"the Silero model loaded but cannot tell speech from a breath: it "
                    f"{heard.describe()}, and both must be on the right side of "
                    f"{self.threshold}. The file or the runtime is wrong."
                )

    def probability(self, frame: np.ndarray) -> float:
        """Silero's speech probability for one frame. Advances the stream."""
        if frame.shape != (self.frame_samples,):
            raise ValueError(f"VAD frame must be {self.frame_samples} samples, got {frame.shape}")
        x = frame.astype(np.float32)
        if frame.dtype == np.int16:
            x /= _INT16_FULL_SCALE
        model_in = np.concatenate((self._context, x[None, :]), axis=1)
        out, self._state = self._session.run(
            None, {"input": model_in, "state": self._state, "sr": self._sr}
        )
        self._context = model_in[:, -self.CONTEXT :]
        self.last_score = float(np.asarray(out).reshape(-1)[0])
        return self.last_score

    def is_speech(self, frame: np.ndarray) -> bool:
        p = self.probability(frame)
        self.last_dbfs = dbfs(frame)
        self._on = p >= (self.exit_threshold if self._on else self.threshold) and (
            self.last_dbfs > self.min_dbfs
        )
        return self._on

    def reset(self) -> None:
        # Only on a real discontinuity (an abort, a reopened device): a cold
        # state scores the next 1-4 frames of speech low.
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)
        self._on = False
        self.last_score = 0.0
        self.last_dbfs = -math.inf

    def self_check(self) -> Hearing:
        """Score a synthetic vowel and breath on a fresh stream, then put the stream back."""
        saved = (self._state, self._context, self._on, self.last_score, self.last_dbfs)
        try:
            pad = np.zeros(4096, dtype=np.int16)
            scores = []
            for sound in (synthetic_vowel(), synthetic_breath()):
                self.reset()
                clip = np.concatenate((pad, sound, pad))
                usable = clip[: clip.size // self.frame_samples * self.frame_samples]
                scores.append(
                    max(self.probability(f) for f in usable.reshape(-1, self.frame_samples))
                )
        finally:
            self._state, self._context, self._on, self.last_score, self.last_dbfs = saved
        return Hearing(vowel=scores[0], breath=scores[1], threshold=self.threshold)


def _silero_session(model_path: str | Path | None) -> Any:
    if model_path is None:
        raise VadUnavailable("no Silero model path was given")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise VadUnavailable('onnxruntime is not installed: pip install -e ".[wake]"') from exc
    opts = ort.SessionOptions()
    # One thread each: this runs inside the PortAudio callback, ~0.2 ms a
    # frame, and a model that fans out over every core is a dropout.
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    try:
        return ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
    except Exception as exc:  # noqa: BLE001 - every load failure means the same thing here
        raise VadUnavailable(f"onnxruntime could not load {model_path}: {exc}") from exc


def voicing(frame: np.ndarray, lags: np.ndarray) -> float:
    """Peak normalised autocorrelation over ``lags``: how periodic the frame is.

    A voice is periodic at its pitch; breath, fans and keyboard clicks are not.
    Computed through one FFT rather than a lag-by-lag loop, because the loop
    cost 0.43 ms a frame and this runs in the audio callback.
    """
    x = frame.astype(np.float64)
    x -= x.mean()
    energy = float(np.dot(x, x))
    if energy <= 0.0:
        return 0.0
    n = x.size
    spectrum = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(spectrum.real**2 + spectrum.imag**2, 2 * n)[:n]
    # Energy of x[lag:], for every lag at once.
    tail = np.cumsum(np.square(x)[::-1])[::-1]
    return float(np.max(ac[lags] / np.sqrt(energy * tail[lags] + 1e-9)))


class VoicedEnergyVad:
    """Loud AND voiced: the VAD for a desk with no Silero model. numpy only.

    The energy test alone is the bug being fixed: a headset breath is as loud as
    speech. A breath is not PERIODIC, so this asks for a pitch between 50 and
    400 Hz as well. The voicing is held for ``hold_frames`` because consonants
    are loud and unvoiced: "stop" would otherwise lose its "st". Measured with a
    3-of-4 onset: 14/14 words kept, 0/13 breaths and clicks.

    Weaker than Silero against voiced noise (a hum, a TV), which is why it is
    the fallback and why a warning says so.
    """

    def __init__(
        self,
        *,
        frame_samples: int = 512,
        rate: int = 16_000,
        min_dbfs: float = VAD_MIN_DBFS,
        voicing_threshold: float = 0.5,
        hold_frames: int = 6,
    ) -> None:
        self.frame_samples = frame_samples
        self.min_dbfs = min_dbfs
        self.voicing_threshold = voicing_threshold
        # 50-400 Hz covers a deep male voice to a child's.
        self._lags = np.arange(rate // 400, rate // 50 + 1)
        self._recent: deque[float] = deque(maxlen=hold_frames)
        self.reset()

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.shape[0] != self.frame_samples:
            raise ValueError(
                f"VAD frame must be {self.frame_samples} samples, got {frame.shape[0]}"
            )
        self._recent.append(voicing(frame, self._lags))
        self.last_score = max(self._recent)
        self.last_dbfs = dbfs(frame)
        return self.last_dbfs > self.min_dbfs and self.last_score > self.voicing_threshold

    def reset(self) -> None:
        self._recent.clear()
        self.last_score = 0.0
        self.last_dbfs = -math.inf


@runtime_checkable
class Resampler(Protocol):
    """Stateful rate conversion. Stateful matters: a per-block stateless resample
    discontinues the filter at every block edge, which the AEC hears as a click
    train on the reference and never converges against."""

    in_rate: int
    out_rate: int

    def process(self, pcm: np.ndarray) -> np.ndarray: ...


class NullResampler:
    quality = "none"

    def __init__(self, rate: int) -> None:
        self.in_rate = rate
        self.out_rate = rate

    def process(self, pcm: np.ndarray) -> np.ndarray:
        return pcm


# MEASURED ON THIS MACHINE (soxr 1.1.0, 48000 -> 16000, 960-sample blocks, 50
# blocks): the quality setting is a GROUP-DELAY trade, not just a CPU one, and
# the architecture's latency budget accounts for soxr's CPU cost (0.02 ms) but
# not for its delay.
#
#     QQ   0 samples     LQ  100 (6.3 ms)    MQ  240 (15 ms)
#     HQ 341 (21 ms)     VHQ 600 (37 ms)
#
# 21 ms of HQ delay is 14% of the ~150 ms budget to the duck, spent on a filter
# whose only job on this path is to reject above 8 kHz for a VAD, a
# closed-vocabulary spotter and a 16 kHz uplink. LQ also emits near-uniform
# chunks, where HQ alternates 0 / 490 and makes detector framing lumpy. So the
# mic path defaults to LQ and the default is stated rather than inherited.
MIC_PATH_QUALITY = "LQ"

# The PLAYBACK path is a different trade and must not inherit the mic's. LQ was
# chosen above because its only job there is to reject above 8 kHz for a VAD, a
# closed-vocabulary spotter and a 16 kHz uplink, and because 21 ms of HQ group
# delay is 14% of the budget to the duck. Neither argument survives on the desk's
# 24k -> 48k playback leg: this audio is what the user actually hears and what
# the AEC references, its delay sits inside the track queue rather than on the
# barge-in path, and upsampling 24 -> 48 is a handful of microseconds.
PLAYBACK_QUALITY = "HQ"


class SoxrResampler:
    """soxr, imported lazily so a numpy-only box still imports this module."""

    def __init__(self, in_rate: int, out_rate: int, *, quality: str = MIC_PATH_QUALITY) -> None:
        try:
            import soxr
        except Exception as exc:  # noqa: BLE001
            raise ResamplerUnavailable(
                f"soxr is required to convert {in_rate} -> {out_rate}; install the 'voice' extra"
            ) from exc
        self.in_rate = in_rate
        self.out_rate = out_rate
        # Kept so a caller can assert WHICH trade it got: the mic path and the
        # playback path want opposite ends of the delay/quality curve.
        self.quality = quality
        self._stream = soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16", quality=quality)

    def process(self, pcm: np.ndarray) -> np.ndarray:
        out = self._stream.resample_chunk(pcm)
        return np.asarray(out, dtype=np.int16).reshape(-1)


def make_resampler(in_rate: int, out_rate: int, *, quality: str = MIC_PATH_QUALITY) -> Resampler:
    """The only place a resampler is chosen, so 'no resampler on the desk path' is checkable.

    Equal rates return a passthrough object rather than None: callers should not
    have to branch, and a graph assembled at 24 kHz end to end provably contains
    no conversion because every resampler it holds is a :class:`NullResampler`.
    """
    if in_rate == out_rate:
        return NullResampler(in_rate)
    return SoxrResampler(in_rate, out_rate, quality=quality)
