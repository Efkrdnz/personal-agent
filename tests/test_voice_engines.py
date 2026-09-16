"""Engines: bytes out, no device, and a ladder that fails in the right order.

The audio assertions here decode the PCM with numpy and look at the waveform,
because "it returned some bytes" is not the same claim as "it returned 24 kHz
mono int16 that a speaker can play". No device is opened anywhere in this file,
and on this machine no device COULD be: ``import sounddevice`` raises, there is
no ``/dev/snd``, and that is exactly the point of the engine contract.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from jarvis.voice import engines
from jarvis.voice.engines import (
    CHANNELS,
    RATE,
    SAMPLE_WIDTH,
    EdgeEngine,
    EngineFailed,
    EngineUnavailable,
    FakeEngine,
    KokoroEngine,
    base_lang,
    float32_to_pcm16,
    pcm_duration_ms,
    silence,
    tone,
    validate_pcm,
)

VOICE_SOURCES = sorted(Path(__file__).resolve().parents[1].joinpath("jarvis", "voice").glob("*.py"))


def as_int16(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2")


# ───────────────────────────── the device contract ─────────────────────────────


def test_no_voice_module_mentions_an_audio_device() -> None:
    """The reference build's TTS player stopped playback process-globally.

    That is why an engine may not touch a device: stopping a spoken confirmation
    would also tear down the Live session's output stream. The rule is only
    worth anything if it is checkable, so this parses for it.
    """
    banned = {"sounddevice", "pyaudio", "soundcard"}
    for path in VOICE_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned, path
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned, path
            elif isinstance(node, ast.Attribute):
                assert node.attr not in {"OutputStream", "InputStream", "Stream"}, path


def test_importing_the_voice_layer_imports_no_audio_or_model_stack() -> None:
    """In a FRESH interpreter, because ``sys.modules`` is process-wide.

    Asserting on this process's ``sys.modules`` only holds until some other test
    in the same session imports the SDK for a reason of its own — which is an
    ordering accident, not an import-cost regression. A subprocess asks the
    question this test is actually about.
    """
    code = (
        "import sys; import jarvis.voice.engines, jarvis.voice.router,"
        " jarvis.voice.verbatim, jarvis.voice.cache, jarvis.voice.chunk;"
        " banned = ('sounddevice', 'edge_tts', 'google.genai');"
        " print([b for b in banned if any(m == b or m.startswith(b + '.')"
        " for m in sys.modules)])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


# ───────────────────────────── waveform primitives ─────────────────────────────


def test_silence_is_whole_frames_at_the_bus_rate() -> None:
    pcm = silence(100)
    assert len(pcm) == RATE // 10 * SAMPLE_WIDTH * CHANNELS
    assert pcm_duration_ms(pcm) == pytest.approx(100.0)
    assert not as_int16(pcm).any()


def test_the_earcon_tone_is_a_real_880_hz_sine_that_starts_and_ends_at_zero() -> None:
    pcm = tone(150, hz=880.0, amplitude=0.18)
    samples = as_int16(pcm).astype(np.float64)
    assert samples.size == RATE * 150 // 1000

    # Fades: a tone that starts on a non-zero sample is a step function, and a
    # step function through a speaker is an audible click.
    assert abs(samples[0]) < 50
    assert abs(samples[-1]) < 50
    assert np.abs(samples).max() == pytest.approx(0.18 * 32767, rel=0.05)

    freqs = np.fft.rfftfreq(samples.size, 1 / RATE)
    peak = freqs[int(np.argmax(np.abs(np.fft.rfft(samples))))]
    assert peak == pytest.approx(880.0, abs=15.0)


def test_float32_conversion_clamps_instead_of_wrapping() -> None:
    pcm = float32_to_pcm16([0.0, 1.0, -1.0, 2.5, -2.5, 0.5])
    assert list(as_int16(pcm)) == [0, 32767, -32767, 32767, -32767, 16384]


@pytest.mark.parametrize("bad", [b"", b"\x00", bytearray(b"\x01\x02\x03"), "not bytes", 42, None])
def test_validate_pcm_refuses_anything_that_is_not_whole_frames(bad: object) -> None:
    with pytest.raises(EngineFailed):
        validate_pcm("under-test", bad)


def test_base_lang_collapses_regional_tags() -> None:
    assert base_lang("tr-TR") == base_lang("TR") == base_lang(" tr ") == "tr"


# ───────────────────────────── the fake ─────────────────────────────


def test_fake_engine_length_is_a_pure_function_of_the_text() -> None:
    engine = FakeEngine()
    pcm = engine.synth("SQLite", "en")
    assert len(pcm) == engine.nbytes("SQLite")
    assert engine.synth("SQLite", "en") == pcm
    assert len(engine.synth("Postgres", "en")) != len(pcm)
    assert len(pcm) % (SAMPLE_WIDTH * CHANNELS) == 0


def test_fake_engine_can_emit_a_distinguishable_tone() -> None:
    engine = FakeEngine(tone_hz=440.0)
    samples = as_int16(engine.synth("hello", "en")).astype(np.float64)
    freqs = np.fft.rfftfreq(samples.size, 1 / RATE)
    assert freqs[int(np.argmax(np.abs(np.fft.rfft(samples))))] == pytest.approx(440.0, abs=15.0)


def test_fake_engine_models_both_failure_shapes() -> None:
    engine = FakeEngine(fail_on=("boom",), unavailable_on=("nope",), langs=frozenset({"en"}))
    with pytest.raises(EngineFailed):
        engine.synth("boom", "en")
    with pytest.raises(EngineUnavailable):
        engine.synth("nope", "en")
    with pytest.raises(EngineUnavailable):
        engine.synth("anything", "tr")
    assert engine.calls == [("boom", "en"), ("nope", "en")]


# ───────────────────────────── edge-tts ─────────────────────────────


def test_edge_maps_turkish_to_the_voice_the_architecture_picked() -> None:
    engine = EdgeEngine()
    assert engine.voice_for("tr") == "tr-TR-AhmetNeural"
    assert engine.voice_for("tr-TR") == "tr-TR-AhmetNeural"
    assert engine.voice_for("en").startswith("en-")
    with pytest.raises(EngineUnavailable, match="no voice configured"):
        engine.voice_for("ja")


def test_edge_decodes_through_the_injected_halves() -> None:
    fetched: list[tuple[str, str]] = []

    def fetch(text: str, voice: str) -> bytes:
        fetched.append((text, voice))
        return b"ID3-pretend-mp3"

    engine = EdgeEngine(fetch=fetch, decode=lambda mp3: silence(40))
    pcm = engine.synth("Postgres", "tr")
    assert fetched == [("Postgres", "tr-TR-AhmetNeural")]
    assert pcm_duration_ms(pcm) == pytest.approx(40.0)


def test_edge_treats_an_empty_download_and_a_mangled_decode_as_failures() -> None:
    with pytest.raises(EngineFailed, match="no audio"):
        EdgeEngine(fetch=lambda t, v: b"", decode=lambda mp3: silence(10)).synth("x", "en")
    with pytest.raises(EngineFailed, match="whole 16-bit frames"):
        EdgeEngine(fetch=lambda t, v: b"mp3", decode=lambda mp3: b"\x01\x02\x03").synth("x", "en")


def test_edge_without_the_package_declines_rather_than_exploding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import is lazy, so a box with no TTS packages still runs the layer."""
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    with pytest.raises(EngineUnavailable, match="edge-tts is not installed"):
        engines._edge_fetch("hello", "en-US-ChristopherNeural")


def test_missing_ffmpeg_is_unavailable_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engines.shutil, "which", lambda name: None)
    with pytest.raises(EngineUnavailable, match="ffmpeg"):
        engines._ffmpeg_decode(b"mp3")


# ───────────────────────────── kokoro ─────────────────────────────


def test_kokoro_declines_turkish_so_the_ladder_falls_through_to_edge() -> None:
    with pytest.raises(EngineUnavailable, match="speaks"):
        KokoroEngine().voice_for("tr")
    assert KokoroEngine().voice_for("en") == "af_heart"


def test_kokoro_converts_float_chunks_to_bus_pcm() -> None:
    chunks = [[0.0, 0.5], [-0.5, 1.0]]
    engine = KokoroEngine(pipeline=lambda text, voice: chunks)
    assert list(as_int16(engine.synth("hi", "en"))) == [0, 16384, -16384, 32767]


def test_kokoro_producing_nothing_is_a_failure_not_silence() -> None:
    with pytest.raises(EngineFailed, match="no audio"):
        KokoroEngine(pipeline=lambda text, voice: []).synth("hi", "en")


def test_kokoro_without_the_package_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "kokoro", None)
    with pytest.raises(EngineUnavailable, match="kokoro is not installed"):
        engines._kokoro_pipeline("hi", "af_heart")
