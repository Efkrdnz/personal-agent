"""Text in, PCM bytes out. AN ENGINE IS FORBIDDEN FROM TOUCHING A DEVICE.

That single line of contract is what deletes the reference build's worst audio
defect: its TTS player both synthesises AND plays, and its stop path calls the
process-global stop, so stopping a spoken confirmation would also kill the Live
session's output stream — a bug you cannot fix without changing who owns the
device, because any engine that opens its own stream is invisible to the mixer,
invisible to the AEC reference tap, and able to stop somebody else's audio. Here
the only object that ever opens a stream is the one audio bus; engines return
bytes and are pure from the outside.

EVERYTHING IS 24 kHz MONO PCM16 LE. Gemini Live output, Gemini TTS, Kokoro and
edge-tts are all natively 24 kHz, so the rate collision is designed out rather
than managed and THERE IS NO RESAMPLER ON THE DESK PATH. Do not add one: the
single resample in the whole system is 24k→8k mu-law inside the phone sink,
applied once to the already-mixed output.

A LADDER, NOT A PICK. Engines fail in ways that are not bugs — a voice that does
not exist for a language, a package that is not installed, an endpoint that is
down — so failure is split in two. :class:`EngineUnavailable` means "not me, ask
the next one" and :class:`EngineFailed` means "I tried and produced nothing
usable". :class:`~jarvis.voice.verbatim.VerbatimSpeaker` walks the ladder on
either and refuses only when it runs out.

Nothing in this module imports ``sounddevice``, ``numpy`` or a network client at
module scope. The whole layer imports, and its tests run, on a machine with no
sound card, no PortAudio and no API key — which is precisely the machine CI runs
on, and also the machine this was written on.
"""

from __future__ import annotations

import array
import math
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "CHANNELS",
    "DEFAULT_EDGE_VOICES",
    "RATE",
    "SAMPLE_WIDTH",
    "EdgeEngine",
    "Engine",
    "EngineFailed",
    "EngineUnavailable",
    "FakeEngine",
    "KokoroEngine",
    "base_lang",
    "float32_to_pcm16",
    "ms_to_samples",
    "pcm_duration_ms",
    "silence",
    "tone",
    "validate_pcm",
]

RATE = 24_000
SAMPLE_WIDTH = 2
CHANNELS = 1


class EngineUnavailable(RuntimeError):
    """This engine cannot serve this request at all — wrong language, no package.

    Distinct from :class:`EngineFailed` because it is not an error condition: a
    Turkish label reaching an English-only local engine is the ladder working.
    """


class EngineFailed(RuntimeError):
    """The engine ran and did not produce usable audio."""


@runtime_checkable
class Engine(Protocol):
    """``synth(text, lang) -> bytes``. PCM16 LE mono at 24 kHz. No device, ever.

    ``deterministic`` is a claim about the class of the thing, not a measured
    error rate: a G2P plus an acoustic model with NO language model in the path
    cannot omit, reorder, translate or invent, while an LLM-based voice can do
    all four and will do them rarely enough to pass a demo. ``verified`` is the
    escape hatch for a non-deterministic engine that has actually cleared
    ``tools/fidelity_probe.py``; until it does, it is FAITHFUL-tier at best.
    """

    name: str
    deterministic: bool
    verified: bool

    def voice_for(self, lang: str) -> str: ...

    def synth(self, text: str, lang: str) -> bytes: ...


def base_lang(lang: str) -> str:
    """``tr-TR`` and ``tr`` must hit the same voice and the same cache key."""
    return lang.split("-")[0].strip().lower()


def ms_to_samples(ms: float) -> int:
    return int(round(RATE * ms / 1000.0))


def pcm_duration_ms(pcm: bytes) -> float:
    return len(pcm) / (RATE * SAMPLE_WIDTH * CHANNELS) * 1000.0


def validate_pcm(engine_name: str, pcm: object) -> bytes:
    """Refuse audio that is not whole int16 frames.

    An odd byte count means the stream was cut mid-sample, and playing it shifts
    every subsequent sample by one byte: white noise at full scale, into
    somebody's speakers. Cheap to check, unpleasant to discover.
    """
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise EngineFailed(f"{engine_name} returned {type(pcm).__name__}, not bytes")
    data = bytes(pcm)
    if not data:
        raise EngineFailed(f"{engine_name} returned no audio")
    if len(data) % (SAMPLE_WIDTH * CHANNELS):
        raise EngineFailed(
            f"{engine_name} returned {len(data)} bytes, which is not whole 16-bit frames"
        )
    return data


def silence(ms: float) -> bytes:
    return b"\x00\x00" * ms_to_samples(ms)


def tone(ms: float, hz: float = 880.0, amplitude: float = 0.18, fade_ms: float = 15.0) -> bytes:
    """A deterministic sine with short fades.

    The fades are not decoration: a tone that starts at a non-zero sample is a
    step function, and a step function through a speaker is an audible click
    that survives every codec between here and a phone handset.
    """
    n = ms_to_samples(ms)
    fade = max(1, min(ms_to_samples(fade_ms), n // 2))
    peak = max(0.0, min(1.0, amplitude)) * 32767.0
    out = array.array("h", bytes(n * SAMPLE_WIDTH))
    for i in range(n):
        gain = 1.0
        if i < fade:
            gain = i / fade
        elif i >= n - fade:
            gain = (n - 1 - i) / fade
        out[i] = int(peak * gain * math.sin(2.0 * math.pi * hz * i / RATE))
    return out.tobytes()


def float32_to_pcm16(samples: Iterable[float]) -> bytes:
    """Clamp then scale. Kokoro and every other local model speaks float32 [-1, 1].

    Clamping rather than wrapping, because a sample that overflows int16 wraps to
    the opposite rail and a wrapped sample is a loud tick.
    """
    out = array.array("h")
    for s in samples:
        v = -1.0 if s < -1.0 else (1.0 if s > 1.0 else float(s))
        out.append(int(round(v * 32767.0)))
    return out.tobytes()


@dataclass(frozen=True, slots=True)
class FakeEngine:
    """Deterministic audio whose LENGTH is a pure function of the text.

    The point is exact assertions: a test can compute the byte count a clip must
    have before the clip exists, so "did the right string reach the reader?" is
    answered by arithmetic rather than by listening. ``fail_on`` makes the ladder
    testable — an engine that refuses specific strings is the shape of every real
    failure (a missing voice, a rate limit, a word the model chokes on).
    """

    name: str = "fake"
    voice: str = "fake-voice"
    deterministic: bool = True
    verified: bool = True
    ms_per_char: float = 55.0
    lead_ms: float = 120.0
    langs: frozenset[str] | None = None
    fail_on: tuple[str, ...] = ()
    unavailable_on: tuple[str, ...] = ()
    tone_hz: float | None = None
    calls: list[tuple[str, str]] = field(default_factory=list, compare=False)

    def voice_for(self, lang: str) -> str:
        if self.langs is not None and base_lang(lang) not in self.langs:
            raise EngineUnavailable(f"{self.name} has no voice for {lang!r}")
        return self.voice

    def duration_ms(self, text: str) -> float:
        return self.lead_ms + self.ms_per_char * len(text)

    def nbytes(self, text: str) -> int:
        return ms_to_samples(self.duration_ms(text)) * SAMPLE_WIDTH * CHANNELS

    def synth(self, text: str, lang: str) -> bytes:
        self.voice_for(lang)
        self.calls.append((text, lang))
        if text in self.unavailable_on:
            raise EngineUnavailable(f"{self.name} declines {text!r}")
        if text in self.fail_on:
            raise EngineFailed(f"{self.name} failed on {text!r}")
        ms = self.duration_ms(text)
        if self.tone_hz is not None:
            return tone(ms, hz=self.tone_hz)
        return silence(ms)


#: edge-tts default output is ``audio-24khz-48kbitrate-mono-mp3``: already the
#: bus rate, so decoding never resamples. ``tr-TR-AhmetNeural`` is the Turkish
#: voice the architecture picked; the English entry is the FALLBACK below a
#: local engine, not the primary.
DEFAULT_EDGE_VOICES: Mapping[str, str] = {
    "tr": "tr-TR-AhmetNeural",
    "en": "en-US-ChristopherNeural",
}

_FFMPEG_ARGS: tuple[str, ...] = (
    "-hide_banner",
    "-loglevel",
    "error",
    "-i",
    "pipe:0",
    "-f",
    "s16le",
    "-acodec",
    "pcm_s16le",
    "-ac",
    str(CHANNELS),
    "-ar",
    str(RATE),
    "pipe:1",
)


@dataclass(frozen=True, slots=True)
class EdgeEngine:
    """Microsoft Edge's voices: free, deterministic, network-bound, MP3 out.

    Two injectable halves, because the network half and the decode half fail for
    unrelated reasons and a test must be able to exercise one without the other.
    ``fetch`` returns the MP3 bytes; ``decode`` turns them into PCM16.

    ``import edge_tts`` is LAZY and lives inside ``fetch``. The module must
    import on a box with no TTS packages at all, because the spine's own tests
    run there and because a voice daemon that cannot start without a network
    package is a voice daemon that cannot tell you its network is down.

    The default ``fetch`` calls ``asyncio.run`` even though ``synth`` is
    synchronous. That is safe and deliberate: the speaker calls every engine via
    ``asyncio.to_thread``, so this runs on a worker thread with no running loop,
    and edge-tts's own async client gets a private loop that dies with the call.
    """

    name: str = "edge"
    deterministic: bool = True
    verified: bool = True
    voices: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_EDGE_VOICES))
    fetch: Callable[[str, str], bytes] | None = None
    decode: Callable[[bytes], bytes] | None = None

    def voice_for(self, lang: str) -> str:
        try:
            return self.voices[base_lang(lang)]
        except KeyError:
            raise EngineUnavailable(f"{self.name} has no voice configured for {lang!r}") from None

    def synth(self, text: str, lang: str) -> bytes:
        voice = self.voice_for(lang)
        mp3 = (self.fetch or _edge_fetch)(text, voice)
        if not mp3:
            raise EngineFailed(f"{self.name} returned no audio for {voice}")
        return validate_pcm(self.name, (self.decode or _ffmpeg_decode)(mp3))


def _edge_fetch(text: str, voice: str) -> bytes:
    import asyncio

    try:
        import edge_tts  # noqa: PLC0415 - lazy on purpose; see EdgeEngine
    except ImportError as exc:
        raise EngineUnavailable("edge-tts is not installed") from exc

    async def pull() -> bytes:
        chunks: list[bytes] = []
        async for item in edge_tts.Communicate(text, voice).stream():
            if item.get("type") == "audio":
                chunks.append(item["data"])
        return b"".join(chunks)

    try:
        return asyncio.run(pull())
    except EngineUnavailable:
        raise
    except Exception as exc:
        raise EngineFailed(f"edge-tts failed for {voice}: {exc}") from exc


def _ffmpeg_decode(mp3: bytes) -> bytes:
    """MP3 → PCM16 24 kHz mono, out of process.

    ``-ar 24000`` is an assertion, not a resample: edge-tts's default format is
    already 24 kHz, so this is a no-op that fails loudly the day that changes.
    """
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise EngineUnavailable("ffmpeg is not on PATH, so MP3 cannot be decoded")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, input is bytes on a pipe
        [exe, *_FFMPEG_ARGS],
        input=mp3,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise EngineFailed(f"ffmpeg exited {proc.returncode}: {proc.stderr.decode()[:200]}")
    return proc.stdout


@dataclass(frozen=True, slots=True)
class KokoroEngine:
    """Local, 24 kHz native, no language model anywhere in the path.

    THIS ADAPTER IS A STUB and says so: the package is not installed on the
    machine it was written on, so ``pipeline`` — the lazy import and the call
    shape — is UNVERIFIED against the real ``kokoro`` API and is the one thing
    here that must be checked against it before this engine goes first in a
    ladder. Everything below the pipeline (the float32→int16 conversion, the
    frame validation, the language gate, the failure classes) is real and
    tested, so wiring the true call is a small, contained change.

    English only, deliberately: a Turkish label must fall THROUGH to edge-tts
    rather than be read by an English voice, and the ladder does that by
    treating a missing language as :class:`EngineUnavailable`.
    """

    name: str = "kokoro"
    deterministic: bool = True
    verified: bool = True
    voice: str = "af_heart"
    langs: frozenset[str] = frozenset({"en"})
    pipeline: Callable[[str, str], Iterable[Sequence[float]]] | None = None

    def voice_for(self, lang: str) -> str:
        if base_lang(lang) not in self.langs:
            raise EngineUnavailable(f"{self.name} speaks {sorted(self.langs)}, not {lang!r}")
        return self.voice

    def synth(self, text: str, lang: str) -> bytes:
        voice = self.voice_for(lang)
        pipe = self.pipeline or _kokoro_pipeline
        chunks = list(pipe(text, voice))
        if not chunks:
            raise EngineFailed(f"{self.name} produced no audio for {voice}")
        out = bytearray()
        for chunk in chunks:
            out += float32_to_pcm16(chunk)
        return validate_pcm(self.name, bytes(out))


def _kokoro_pipeline(text: str, voice: str) -> Iterable[Sequence[float]]:
    try:
        from kokoro import KPipeline  # noqa: PLC0415 - lazy; the package is optional
    except ImportError as exc:
        raise EngineUnavailable("kokoro is not installed") from exc
    try:
        pipeline = KPipeline(lang_code="a")
        return [audio for _, _, audio in pipeline(text, voice=voice)]
    except Exception as exc:
        raise EngineFailed(f"kokoro failed for {voice}: {exc}") from exc
