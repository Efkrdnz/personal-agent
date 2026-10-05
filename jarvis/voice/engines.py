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
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "CHANNELS",
    "DEFAULT_EDGE_VOICES",
    "DEFAULT_SAPI_VOICES",
    "RATE",
    "SAMPLE_WIDTH",
    "DEFAULT_SYSTEM_VOICES",
    "EdgeEngine",
    "Engine",
    "EngineFailed",
    "EngineUnavailable",
    "FakeEngine",
    "GEMINI_TTS_MODEL",
    "GeminiTtsEngine",
    "KokoroEngine",
    "SystemEngine",
    "base_lang",
    "float32_to_pcm16",
    "ms_to_samples",
    "pcm_duration_ms",
    "silence",
    "to_24k",
    "tone",
    "validate_pcm",
    "wav_to_pcm",
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
#: local engine, not the primary. Ryan is a British man, the nearest Edge has to
#: the butler, and the same default as ``voice.reader_voice`` in the config.
DEFAULT_EDGE_VOICES: Mapping[str, str] = {
    "tr": "tr-TR-AhmetNeural",
    "en": "en-GB-RyanNeural",
}

#: CREATE_NO_WINDOW. The app runs with no console, and a console program it
#: starts (PowerShell, ffmpeg, espeak-ng) would otherwise be given a NEW console
#: window of its own: a black box flashing up every time Jarvis reads a line.
_CREATE_NO_WINDOW = 0x08000000


def _hidden(platform: str = sys.platform) -> dict[str, int]:
    """Keyword arguments for every helper process this module starts."""
    return {"creationflags": _CREATE_NO_WINDOW} if platform == "win32" else {}


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
        return validate_pcm(self.name, (self.decode or _mp3_decode)(mp3))


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


def _mp3_decode(mp3: bytes) -> bytes:
    """MP3 → PCM16 24 kHz mono: ffmpeg when it is on PATH, else miniaudio in-process.

    Windows ships no ffmpeg, so without the second half the British reader voice
    could never play there. ffmpeg stays first because it is the path that has
    run longest. With neither, :class:`EngineUnavailable` names both fixes and
    the ladder moves on to the system voice.
    """
    if shutil.which("ffmpeg") is not None:
        return _ffmpeg_decode(mp3)
    try:
        import miniaudio  # noqa: PLC0415 - optional, lazy, like edge_tts
    except ImportError as exc:
        raise EngineUnavailable(
            "MP3 cannot be decoded: put ffmpeg on PATH, or install miniaudio "
            '(pip install miniaudio, or the ".[tts]" extra)'
        ) from exc
    return _miniaudio_decode(mp3, miniaudio)


def _miniaudio_decode(mp3: bytes, miniaudio: object) -> bytes:
    """The in-process decoder. Asked for the bus format, so it is never resampled again.

    ``sample_rate=24000`` and ``nchannels=1`` are requests miniaudio honours by
    converting; for edge-tts's own 24 kHz mono they are no-ops, as with ffmpeg.
    miniaudio can also open a device; only its decoder is used, and the rule at
    the top of this module stands.
    """
    try:
        decoded = miniaudio.decode(  # type: ignore[attr-defined]
            # bytes(), because its cffi layer refuses a bytearray outright, and
            # "the download was a bytearray" is not a reason to lose the voice.
            bytes(mp3),
            output_format=miniaudio.SampleFormat.SIGNED16,  # type: ignore[attr-defined]
            nchannels=CHANNELS,
            sample_rate=RATE,
        )
    except Exception as exc:  # noqa: BLE001 - a corrupt download is "next rung", whatever raised
        raise EngineFailed(f"miniaudio could not decode the MP3: {exc}") from exc
    samples = array.array("h", decoded.samples)
    if sys.byteorder != "little":
        # The bus is little-endian by contract; array.tobytes() is native order.
        samples.byteswap()
    return samples.tobytes()


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
        **_hidden(),
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


# ───────────────────────────── built-in: the OS's own voice ─────────────────────────────


def wav_to_pcm(wav: bytes) -> tuple[bytes, int]:
    """(PCM16 mono bytes, sample rate) from a RIFF/WAVE blob. Trusts the bytes, not the header.

    A streaming writer — espeak-ng on stdout, for one — does not know the length
    when it writes the header, so it puts 0x7FFFF... in both size fields. The
    stdlib ``wave`` module believes them and reports a billion frames. Here the
    data chunk runs to whatever actually arrived.
    """
    if len(wav) < 12 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise EngineFailed("not a WAV file")
    pos, rate, channels, bits = 12, 0, 0, 0
    while pos + 8 <= len(wav):
        cid = wav[pos : pos + 4]
        size = int.from_bytes(wav[pos + 4 : pos + 8], "little")
        body = pos + 8
        if cid == b"fmt ":
            fmt = int.from_bytes(wav[body : body + 2], "little")
            channels = int.from_bytes(wav[body + 2 : body + 4], "little")
            rate = int.from_bytes(wav[body + 4 : body + 8], "little")
            bits = int.from_bytes(wav[body + 14 : body + 16], "little")
            if fmt not in (1, 0xFFFE):
                raise EngineFailed(f"WAV format {fmt} is not PCM")
        elif cid == b"data":
            end = min(len(wav), body + size)
            data = wav[body:end]
            if bits != 16:
                raise EngineFailed(f"{bits}-bit WAV, expected 16")
            if channels == 2:
                pcm = array.array("h", data[: len(data) // 4 * 4])
                data = array.array("h", pcm[0::2]).tobytes()
            elif channels != 1:
                raise EngineFailed(f"{channels}-channel WAV")
            return data[: len(data) // 2 * 2], rate
        pos = body + size + (size & 1)
    raise EngineFailed("WAV has no data chunk")


def to_24k(pcm: bytes, rate: int) -> bytes:
    """Linear-interpolate PCM16 mono to 24 kHz. ONCE, at synthesis, inside the engine.

    The module rule — no resampler on the desk path — is about the mixed output
    stream. This runs when a clip is made, before the cache, so the desk path
    still only ever sees 24 kHz. Linear is enough for this: upsampling 22.05k to
    24k cannot alias, and the voices that need it are not hi-fi to begin with.
    """
    if rate == RATE or not pcm:
        return pcm
    src = array.array("h", pcm)
    n_out = int(len(src) * RATE / rate)
    out = array.array("h", bytes(n_out * SAMPLE_WIDTH))
    step = rate / RATE
    last = len(src) - 1
    for i in range(n_out):
        x = i * step
        j = int(x)
        if j >= last:
            out[i] = src[last]
            continue
        frac = x - j
        out[i] = int(src[j] + (src[j + 1] - src[j]) * frac)
    return out.tobytes()


#: The OS voices, per language. espeak-ng names its voices by locale, and
#: ``en-gb-x-rp`` is its Received Pronunciation: the nearest thing it has to a
#: butler. Present in espeak-ng 1.51 here (``espeak-ng --voices=en``); where it is
#: not, :data:`_ESPEAK_FALLBACK` asks for plain ``en-gb`` instead.
DEFAULT_SYSTEM_VOICES: Mapping[str, str] = {"en": "en-gb-x-rp", "tr": "tr"}

#: What to ask for when the preferred espeak voice is refused. espeak-ng itself
#: resolves an unknown name to the nearest language and speaks anyway; the older
#: espeak, which has no ``-x-rp`` voices, is the one this is for.
_ESPEAK_FALLBACK: Mapping[str, str] = {"en-gb-x-rp": "en-gb"}

#: SAPI voices to prefer, best first, each matched as a substring of an
#: INSTALLED voice's name ("Microsoft George", "Microsoft Hazel Desktop"). The
#: British man first, because that is the character; then the British woman an
#: English (UK) language pack brings; then the American man every Windows has.
#: If none is installed the system default speaks, which is still a voice.
DEFAULT_SAPI_VOICES: tuple[str, ...] = ("George", "Ryan", "Hazel", "David")


def _which_system_tts() -> str | None:
    """espeak-ng, espeak, macOS `say` or Windows PowerShell — whichever this OS has."""
    for name in ("espeak-ng", "espeak", "say"):
        if shutil.which(name):
            return name
    if shutil.which("powershell") or shutil.which("pwsh"):
        return "sapi"
    return None


#: Windows SAPI via PowerShell. The text arrives through an ENVIRONMENT VARIABLE,
#: never through the command line: interpolating a sentence into a PowerShell
#: script is a code-injection bug the first time somebody dictates a semicolon.
#: The voice preferences travel the same way, for the same reason. Three parts
#: so the middle one can be run on its own against a stand-in synthesiser.
_SAPI_LOAD = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
)
#: The first INSTALLED, enabled voice whose name contains a preference, in
#: preference order; nothing matching keeps the default. IndexOf rather than
#: -like, because a preference is data and -like would read [ ] * ? in it as a
#: pattern. A voice that refuses SelectVoice is skipped, not fatal.
_SAPI_PICK = (
    "$want = @(([string]$env:JARVIS_TTS_VOICES) -split ';'"
    " | ForEach-Object { $_.Trim() } | Where-Object { $_ });"
    "$have = @($s.GetInstalledVoices() | Where-Object { $_.Enabled }"
    " | ForEach-Object { $_.VoiceInfo.Name });"
    ":pick foreach ($w in $want) { foreach ($h in $have) {"
    " if ($h.IndexOf($w, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {"
    " try { $s.SelectVoice($h); break pick } catch { } } } };"
)
_SAPI_SPEAK = (
    "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
    "24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
    " [System.Speech.AudioFormat.AudioChannel]::Mono);"
    "$s.SetOutputToWaveFile($env:JARVIS_TTS_OUT, $f);"
    "$s.Speak($env:JARVIS_TTS_TEXT); $s.Dispose()"
)
_SAPI_SCRIPT = _SAPI_LOAD + _SAPI_PICK + _SAPI_SPEAK


@dataclass(frozen=True, slots=True)
class SystemEngine:
    """The operating system's own speech synthesiser. Nothing to download, no network.

    Linux: espeak-ng (``apt install espeak-ng``). macOS: ``say``, always present.
    Windows: SAPI through PowerShell, always present. Rule-based synthesis with
    no language model in the path, so it cannot omit, reorder or invent a word —
    it is DETERMINISTIC in exactly the sense the exact tier needs, and it is the
    rung that means the desk can read an option label with nothing installed.

    The text is handed over on STDIN (or an environment variable for SAPI),
    never as an argument: a label that starts with "-" would otherwise be read
    as a flag, and one containing a quote would end the string early.
    """

    name: str = "system"
    deterministic: bool = True
    verified: bool = True
    voices: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_SYSTEM_VOICES))
    binary: str | None = None
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
    #: Windows only: which installed SAPI voice to use. See DEFAULT_SAPI_VOICES.
    sapi_voices: tuple[str, ...] = DEFAULT_SAPI_VOICES

    def __post_init__(self) -> None:
        for want in self.sapi_voices:
            if not want.strip() or ";" in want:
                # ";" separates them on the way to PowerShell, so one inside a
                # name would quietly become two preferences.
                raise ValueError(f"a SAPI voice preference must be a plain name, got {want!r}")

    def _binary(self) -> str:
        found = self.binary or _which_system_tts()
        if found is None:
            raise EngineUnavailable(
                "no system voice: install espeak-ng (Linux), or use macOS/Windows"
            )
        return found

    def voice_for(self, lang: str) -> str:
        base = base_lang(lang)
        binary = self._binary()
        if binary in ("say", "sapi"):
            # Their default voice follows the OS language; asking for a named
            # one that is not installed fails, so English is the only promise.
            if base != "en":
                raise EngineUnavailable(f"{binary} is only trusted for English here")
            return "default"
        try:
            return self.voices[base]
        except KeyError:
            raise EngineUnavailable(f"no system voice configured for {lang!r}") from None

    def synth(self, text: str, lang: str) -> bytes:
        voice = self.voice_for(lang)
        binary = self._binary()
        if binary in ("espeak-ng", "espeak"):
            return self._espeak(binary, voice, text)
        return self._via_file(binary, text)

    def _espeak(self, binary: str, voice: str, text: str) -> bytes:
        why = ""
        for each in (voice, *((_ESPEAK_FALLBACK[voice],) if voice in _ESPEAK_FALLBACK else ())):
            proc = self.run(
                [binary, "--stdin", "--stdout", "-v", each, "-s", "165"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=30,
                check=False,
                **_hidden(),
            )
            if proc.returncode == 0 and proc.stdout:
                pcm, rate = wav_to_pcm(proc.stdout)
                return validate_pcm(self.name, to_24k(pcm, rate))
            why = proc.stderr.decode(errors="replace")[:200]
        raise EngineFailed(f"{binary} failed: {why}")

    def _via_file(self, binary: str, text: str) -> bytes:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "speech.wav")
            if binary == "say":
                proc = self.run(
                    ["say", "-o", out, "--data-format=LEI16@24000", "-f", "-"],
                    input=text.encode("utf-8"),
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            else:
                shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
                proc = self.run(
                    [shell, "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT],
                    env={
                        **os.environ,
                        "JARVIS_TTS_TEXT": text,
                        "JARVIS_TTS_OUT": out,
                        "JARVIS_TTS_VOICES": ";".join(self.sapi_voices),
                    },
                    capture_output=True,
                    timeout=30,
                    check=False,
                    **_hidden(),
                )
            if proc.returncode != 0 or not os.path.exists(out):
                raise EngineFailed(f"{binary} failed: {proc.stderr.decode(errors='replace')[:200]}")
            with open(out, "rb") as fh:
                pcm, rate = wav_to_pcm(fh.read())
        return validate_pcm(self.name, to_24k(pcm, rate))


# ───────────────────────────── Gemini TTS ─────────────────────────────

#: From google-genai 2.23.0's own test suite. READ, not measured.
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"


@dataclass(frozen=True, slots=True)
class GeminiTtsEngine:
    """Gemini's speech model: the same key as the conversation, natural, 24 kHz.

    NOT DETERMINISTIC, and so never trusted with EXACT text. It is a generative
    voice — it reads, but it is the kind of reader that can smooth a word or
    drop one — and ``VerbatimSpeaker`` bars it from answer keys until
    ``tools/fidelity_probe.py`` has measured it. For everything else it is the
    best-sounding rung most installs will have, with nothing extra to install.

    ``google.genai`` is imported inside :meth:`synth`; the key is a parameter.
    """

    api_key: str
    model: str = GEMINI_TTS_MODEL
    voice: str = "Kore"
    name: str = "gemini"
    deterministic: bool = False
    verified: bool = False
    client: object | None = None

    def voice_for(self, lang: str) -> str:
        return self.voice

    def synth(self, text: str, lang: str) -> bytes:
        client = self.client
        if client is None:
            if not self.api_key:
                raise EngineUnavailable("no Gemini key for speech")
            try:
                from google import genai  # noqa: PLC0415 - optional, lazy
            except ImportError as exc:
                raise EngineUnavailable("google-genai is not installed") from exc
            client = genai.Client(api_key=self.api_key)
        try:
            reply = client.models.generate_content(  # type: ignore[attr-defined]
                model=self.model,
                contents=text,
                config={
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}}
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - every SDK failure means "next rung"
            raise EngineFailed(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        blob = None
        for cand in getattr(reply, "candidates", None) or ():
            for part in getattr(getattr(cand, "content", None), "parts", None) or ():
                if getattr(part, "inline_data", None) is not None:
                    blob = part.inline_data
                    break
            if blob is not None:
                break
        if blob is None or not getattr(blob, "data", None):
            raise EngineFailed(f"{self.name} returned no audio")
        data = bytes(blob.data)
        mime = str(getattr(blob, "mime_type", "") or "")
        if data[:4] == b"RIFF":
            pcm, rate = wav_to_pcm(data)
        else:
            # "audio/L16;codec=pcm;rate=24000" — raw little-endian PCM16.
            rate = RATE
            for piece in mime.split(";"):
                if piece.strip().startswith("rate="):
                    rate = int(piece.split("=", 1)[1])
            pcm = data
        return validate_pcm(self.name, to_24k(pcm, rate))
