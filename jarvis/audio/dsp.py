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
from typing import Protocol, runtime_checkable

import numpy as np

__all__ = [
    "AecUnavailable",
    "EchoCanceller",
    "EnergyVad",
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
    "WebRtcEchoCanceller",
    "dbfs",
    "erle_db",
    "make_resampler",
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


class SileroVad:
    """Silero through sherpa-onnx, behind the same protocol.

    Do NOT ``pip install silero-vad``: it drags torch in even for the ONNX path.
    sherpa-onnx bundles the same model and is the runtime the keyword spotters
    already need, so it is one dependency for four features.
    """

    def __init__(self, model_path: str, *, frame_samples: int = 512, threshold: float = 0.5):
        try:
            import sherpa_onnx
        except Exception as exc:  # noqa: BLE001
            raise VadUnavailable(
                "sherpa-onnx is not installed; EnergyVad is the supported fallback"
            ) from exc
        self.frame_samples = frame_samples
        self._threshold = threshold
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = model_path
        config.silero_vad.threshold = threshold
        config.sample_rate = 16_000
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=1.0)

    def is_speech(self, frame: np.ndarray) -> bool:
        self._vad.accept_waveform(frame.astype(np.float32) / _INT16_FULL_SCALE)
        return bool(self._vad.is_speech_detected())

    def reset(self) -> None:
        self._vad.reset()


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
