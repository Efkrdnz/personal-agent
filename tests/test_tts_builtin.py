"""The built-in voices: the OS's own synthesiser, and Gemini's.

The system engine is tested twice: for real against espeak-ng where it exists
(it does in CI images that install it, and on the machine this was written on),
and against a fake ``run`` for the macOS and Windows paths, which is where the
injection risks live.
"""

from __future__ import annotations

import array
import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

from jarvis.voice import engines as eng
from jarvis.voice.verbatim import NoVerbatimEngine, VerbatimSpeaker


def a_wav(rate: int, samples: list[int], *, streaming: bool = False, channels: int = 1) -> bytes:
    data = array.array("h", samples).tobytes()
    size = 0x7FFFFFFF if streaming else len(data)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 0x7FFFFFFF if streaming else 36 + len(data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", size)
        + data
    )


# ───────────────────────────── WAV handling ─────────────────────────────


def test_a_streaming_wav_is_read_to_its_real_end_not_its_claimed_one() -> None:
    """espeak-ng on stdout claims 2 GB. The stdlib `wave` module believes it."""
    pcm, rate = eng.wav_to_pcm(a_wav(22050, [1, 2, 3, 4], streaming=True))
    assert rate == 22050
    assert array.array("h", pcm).tolist() == [1, 2, 3, 4]


def test_stereo_is_folded_to_mono() -> None:
    pcm, _ = eng.wav_to_pcm(a_wav(24000, [10, -10, 20, -20], channels=2))
    assert array.array("h", pcm).tolist() == [10, 20]


def test_something_that_is_not_a_wav_is_refused() -> None:
    with pytest.raises(eng.EngineFailed):
        eng.wav_to_pcm(b"ID3 an mp3, not a wav")


def test_resampling_lands_on_24k_and_keeps_the_duration() -> None:
    src = array.array("h", [int(500 * (i % 50)) for i in range(22050)]).tobytes()
    out = eng.to_24k(src, 22050)
    assert len(out) // 2 == 24000
    assert eng.to_24k(src, eng.RATE) is src, "24k in must be passed through untouched"


# ───────────────────────────── the system engine, for real ─────────────────────────────

needs_espeak = pytest.mark.skipif(
    not (shutil.which("espeak-ng") or shutil.which("espeak")), reason="no espeak-ng here"
)


@needs_espeak
def test_espeak_speaks_english_and_turkish_at_24k() -> None:
    e = eng.SystemEngine()
    for lang, text in (("en", "Two should say Postgres."), ("tr", "Yarın yağmur var.")):
        pcm = e.synth(text, lang)
        ms = eng.pcm_duration_ms(pcm)
        assert 400 < ms < 6000, (lang, ms)
        samples = array.array("h", pcm)
        assert max(abs(s) for s in samples) > 1000, "silence is not speech"


@needs_espeak
def test_a_label_that_looks_like_a_flag_is_spoken_not_obeyed() -> None:
    """Text goes in on stdin. As an argument, "--help" would print help and say nothing."""
    pcm = eng.SystemEngine().synth("--help", "en")
    assert eng.pcm_duration_ms(pcm) > 200


@needs_espeak
def test_the_system_engine_can_read_exact_text() -> None:
    """The point of it: an answer key read aloud with nothing extra installed."""
    import asyncio

    speaker = VerbatimSpeaker(engines=(eng.SystemEngine(),))
    pcm = asyncio.run(speaker.pcm_for("2. Postgres", "en", exact=True))
    assert pcm


# ───────────────────────────── macOS and Windows, faked ─────────────────────────────


class FakeRun:
    def __init__(self, wav: bytes) -> None:
        self.wav = wav
        self.calls: list[dict] = []

    def __call__(self, argv, **kw):  # noqa: ANN001
        self.calls.append({"argv": list(argv), **kw})
        out = (kw.get("env") or {}).get("JARVIS_TTS_OUT")
        if out is None and "-o" in argv:
            out = argv[argv.index("-o") + 1]
        if out:
            with open(out, "wb") as fh:
                fh.write(self.wav)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_macos_say_gets_the_text_on_stdin() -> None:
    run = FakeRun(a_wav(24000, [100, -100] * 2400))
    text = 'He said "rm -rf" -- then left'
    eng.SystemEngine(binary="say", run=run).synth(text, "en")
    call = run.calls[0]
    assert call["input"] == text.encode()
    assert text not in " ".join(call["argv"]), "the text must never be an argument"


def test_windows_sapi_gets_the_text_through_the_environment() -> None:
    """Interpolating a sentence into a PowerShell script is code injection."""
    run = FakeRun(a_wav(24000, [100, -100] * 2400))
    text = "'; Remove-Item -Recurse C:\\ ; '"
    eng.SystemEngine(binary="sapi", run=run).synth(text, "en")
    call = run.calls[0]
    assert call["env"]["JARVIS_TTS_TEXT"] == text
    assert text not in " ".join(call["argv"])
    assert "$env:JARVIS_TTS_TEXT" in " ".join(call["argv"])


def test_say_and_sapi_do_not_pretend_to_speak_turkish() -> None:
    """Their default voice follows the OS language; a Turkish label must fall through."""
    for binary in ("say", "sapi"):
        with pytest.raises(eng.EngineUnavailable):
            eng.SystemEngine(binary=binary).voice_for("tr")


def test_no_system_voice_is_unavailable_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable means "ask the next rung"; it must not stop the ladder."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(eng.EngineUnavailable, match="espeak-ng"):
        eng.SystemEngine().voice_for("en")


# ───────────────────────────── Gemini TTS ─────────────────────────────


def gemini_reply(data: bytes, mime: str = "audio/L16;codec=pcm;rate=24000") -> SimpleNamespace:
    part = SimpleNamespace(inline_data=SimpleNamespace(data=data, mime_type=mime))
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


class FakeClient:
    def __init__(self, reply: SimpleNamespace) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.models = self

    def generate_content(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        return self.reply


def test_gemini_tts_asks_for_audio_in_the_configured_voice() -> None:
    client = FakeClient(gemini_reply(b"\x01\x00" * 4800))
    e = eng.GeminiTtsEngine(api_key="k", voice="Charon", client=client)
    pcm = e.synth("Hello there.", "en")
    assert len(pcm) == 9600
    cfg = client.calls[0]["config"]
    assert cfg["response_modalities"] == ["AUDIO"]
    assert cfg["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"] == "Charon"
    assert client.calls[0]["model"] == eng.GEMINI_TTS_MODEL


def test_gemini_tts_at_another_rate_is_brought_to_24k() -> None:
    client = FakeClient(gemini_reply(b"\x01\x00" * 16000, "audio/L16;codec=pcm;rate=16000"))
    pcm = eng.GeminiTtsEngine(api_key="k", client=client).synth("x", "en")
    assert len(pcm) // 2 == 24000


def test_gemini_tts_with_no_audio_is_a_failure_not_silence() -> None:
    empty = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))])
    with pytest.raises(eng.EngineFailed, match="no audio"):
        eng.GeminiTtsEngine(api_key="k", client=FakeClient(empty)).synth("x", "en")


def test_gemini_tts_is_never_trusted_with_an_answer_key() -> None:
    """A generative voice can smooth or drop a word. Exact text needs a deterministic rung."""
    import asyncio

    client = FakeClient(gemini_reply(b"\x01\x00" * 4800))
    speaker = VerbatimSpeaker(engines=(eng.GeminiTtsEngine(api_key="k", client=client),))
    with pytest.raises(NoVerbatimEngine):
        asyncio.run(speaker.pcm_for("2. Postgres", "en", exact=True))
    assert client.calls == [], "it must be barred before it is even asked"
    assert asyncio.run(speaker.pcm_for("Good morning.", "en", exact=False))
