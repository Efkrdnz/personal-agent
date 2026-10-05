"""The built-in voices: the OS's own synthesiser, and Gemini's.

The system engine is tested twice: for real against espeak-ng where it exists
(it does in CI images that install it, and on the machine this was written on),
and against a fake ``run`` for the macOS and Windows paths, which is where the
injection risks live.
"""

from __future__ import annotations

import array
import base64
import itertools
import re
import shutil
import struct
import subprocess
import sys
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


# ───────────────────────────── the Jarvis voices: SAPI, espeak, MP3 ─────────────────────────────


def test_sapi_voice_preferences_travel_by_environment_never_argv() -> None:
    """The preference list is data, like the text, so it takes the same road."""
    run = FakeRun(a_wav(24000, [100, -100] * 2400))
    eng.SystemEngine(binary="sapi", run=run, sapi_voices=("George", "Hazel")).synth("Hi.", "en")
    call = run.calls[0]
    assert call["env"]["JARVIS_TTS_VOICES"] == "George;Hazel"
    argv = " ".join(call["argv"])
    assert "George" not in argv and "Hazel" not in argv
    assert "$env:JARVIS_TTS_VOICES" in argv


def test_the_default_sapi_preference_is_a_british_man_first() -> None:
    run = FakeRun(a_wav(24000, [100, -100] * 2400))
    eng.SystemEngine(binary="sapi", run=run).synth("Good evening.", "en")
    assert run.calls[0]["env"]["JARVIS_TTS_VOICES"] == "George;Ryan;Hazel;David"
    assert eng.DEFAULT_SAPI_VOICES[0] == "George"


def test_the_sapi_script_only_selects_an_installed_voice() -> None:
    """Asking SAPI for a voice that is not installed throws; picking from the list cannot."""
    script = eng._SAPI_SCRIPT
    assert script == eng._SAPI_LOAD + eng._SAPI_PICK + eng._SAPI_SPEAK
    assert "GetInstalledVoices()" in script and "SelectVoice($h)" in script
    # -like would treat [ ] * ? in a preference as a pattern; a preference is data.
    assert "-like" not in eng._SAPI_PICK and "IndexOf" in eng._SAPI_PICK
    # No double quote: on a Windows command line it needs escaping that
    # powershell.exe and Python's list2cmdline do not agree on.
    assert '"' not in script
    # Every value from outside arrives through $env:, and only these three do.
    assert set(re.findall(r"\$env:(\w+)", script)) == {
        "JARVIS_TTS_TEXT",
        "JARVIS_TTS_OUT",
        "JARVIS_TTS_VOICES",
    }


def test_a_sapi_preference_that_would_split_in_two_is_refused() -> None:
    with pytest.raises(ValueError, match="plain name"):
        eng.SystemEngine(sapi_voices=("George;David",))
    with pytest.raises(ValueError, match="plain name"):
        eng.SystemEngine(sapi_voices=(" ",))


def test_helper_processes_never_open_a_console_window_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A windowless app that starts PowerShell gets a black box flashing up per sentence."""
    assert eng._hidden("win32") == {"creationflags": 0x08000000}
    assert eng._hidden("linux") == {} and eng._hidden("darwin") == {}
    monkeypatch.setattr(eng, "_hidden", lambda: {"creationflags": 0x08000000})
    run = FakeRun(a_wav(24000, [100, -100] * 2400))
    eng.SystemEngine(binary="sapi", run=run).synth("Hi.", "en")
    assert run.calls[0]["creationflags"] == 0x08000000
    seen: list[dict] = []

    def espeak(argv, **kw):  # noqa: ANN001
        seen.append(kw)
        return subprocess.CompletedProcess(argv, 0, a_wav(22050, [100, -100] * 2400), b"")

    eng.SystemEngine(binary="espeak-ng", run=espeak).synth("Hi.", "en")
    assert seen[0]["creationflags"] == 0x08000000


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


#: A stand-in synthesiser with the two methods the pick uses. The installed
#: voices come in as JSON through the environment, like everything else. The
#: parentheses around ConvertFrom-Json are for Windows PowerShell 5.1, which
#: otherwise passes a JSON array down the pipeline as ONE object. Single quotes
#: only, as in the real script: a double quote on a Windows command line needs
#: escaping that powershell.exe and Python do not agree on.
_FAKE_SAPI = (
    "$s = [pscustomobject]@{ Picked = $null };"
    "$s | Add-Member -MemberType ScriptMethod -Name GetInstalledVoices -Value {"
    " (ConvertFrom-Json $env:FAKE_VOICES) | ForEach-Object { [pscustomobject]@{"
    " Enabled = $_.enabled; VoiceInfo = [pscustomobject]@{ Name = $_.name } } } };"
    "$s | Add-Member -MemberType ScriptMethod -Name SelectVoice -Value { param($n)"
    " if ($n -like '*Broken*') { throw 'refused' }; $this.Picked = $n };"
)


@pytest.mark.skipif(_powershell() is None, reason="no PowerShell here")
@pytest.mark.parametrize(
    ("installed", "want", "picked"),
    [
        ([("Microsoft David Desktop", True), ("Microsoft Zira Desktop", True)], "", "-"),
        ([("Microsoft David Desktop", True), ("Microsoft Hazel Desktop", True)], None, "Hazel"),
        ([("Microsoft David Desktop", True), ("Microsoft Zira Desktop", True)], None, "David"),
        ([("Microsoft Zira Desktop", True)], None, "-"),
        ([("Microsoft George", False), ("Microsoft David Desktop", True)], None, "David"),
        ([("Microsoft George Broken", True), ("Microsoft Hazel", True)], None, "Hazel"),
        ([("Microsoft george", True), ("Microsoft David Desktop", True)], None, "george"),
        ([("Voice [x]*", True)], "[x]*", "[x]*"),
    ],
)
def test_the_sapi_pick_in_real_powershell(
    installed: list[tuple[str, bool]], want: str | None, picked: str
) -> None:
    """The middle of the real script, run by PowerShell against a stand-in synthesiser.

    Runs wherever PowerShell exists, which includes the Windows CI runner: it is
    the only check that the script is PowerShell at all, rather than a string
    that looks like it.
    """
    import json
    import os

    tail = "if ($s.Picked) { 'PICKED:' + $s.Picked } else { 'PICKED:-' }"
    env = {
        **os.environ,
        "FAKE_VOICES": json.dumps([{"name": n, "enabled": e} for n, e in installed]),
        "JARVIS_TTS_VOICES": ";".join(eng.DEFAULT_SAPI_VOICES) if want is None else want,
    }
    proc = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _FAKE_SAPI + eng._SAPI_PICK + tail,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = proc.stdout.strip().rsplit("PICKED:", 1)[-1]
    assert got == "-" if picked == "-" else picked in got, (got, proc.stderr)


@pytest.mark.skipif(_powershell() is None, reason="no PowerShell here")
def test_the_whole_sapi_script_parses_as_powershell() -> None:
    import os

    check = (
        "$e = $null; $t = $null;"
        "[void][System.Management.Automation.Language.Parser]::ParseInput("
        "$env:SCRIPT, [ref]$t, [ref]$e);"
        "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 } else { 'parsed' }"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", check],
        env={**os.environ, "SCRIPT": eng._SAPI_SCRIPT},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0 and "parsed" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="SAPI is Windows only")
def test_real_sapi_speaks_with_the_preferred_voice_list() -> None:
    pcm = eng.SystemEngine(binary="sapi").synth("Good evening. All systems are in order.", "en")
    assert eng.pcm_duration_ms(pcm) > 500
    assert max(abs(s) for s in array.array("h", pcm)) > 500, "silence is not speech"


def test_espeak_asks_for_received_pronunciation_and_falls_back_to_plain_british() -> None:
    assert eng.DEFAULT_SYSTEM_VOICES["en"] == "en-gb-x-rp"

    class Refuses:
        """The older espeak: no -x-rp voice, and a non-zero exit to say so."""

        def __init__(self) -> None:
            self.voices: list[str] = []

        def __call__(self, argv, **kw):  # noqa: ANN001
            voice = argv[argv.index("-v") + 1]
            self.voices.append(voice)
            if voice == "en-gb-x-rp":
                return subprocess.CompletedProcess(argv, 1, b"", b"Failed to read voice")
            return subprocess.CompletedProcess(argv, 0, a_wav(22050, [100, -100] * 2400), b"")

    run = Refuses()
    pcm = eng.SystemEngine(binary="espeak", run=run).synth("Good evening.", "en")
    assert run.voices == ["en-gb-x-rp", "en-gb"]
    assert eng.pcm_duration_ms(pcm) > 100


def test_an_espeak_voice_with_no_fallback_is_tried_once_then_fails() -> None:
    asked: list[str] = []

    def broken(argv, **kw):  # noqa: ANN001
        asked.append(argv[argv.index("-v") + 1])
        return subprocess.CompletedProcess(argv, 1, b"", b"no voice")

    with pytest.raises(eng.EngineFailed, match="espeak-ng failed: no voice"):
        eng.SystemEngine(binary="espeak-ng", run=broken).synth("Yarin yagmur var.", "tr")
    assert asked == ["tr"]


@needs_espeak
def test_the_real_rp_voice_speaks_here() -> None:
    pcm = eng.SystemEngine().synth("Good evening, sir. Eleven degrees and drizzling.", "en")
    assert 1000 < eng.pcm_duration_ms(pcm) < 8000
    assert max(abs(s) for s in array.array("h", pcm)) > 1000


#: 250 ms of a 440 Hz sine, encoded by LAME at edge-tts's own format (24 kHz,
#: 48 kbit/s, mono MPEG-2 layer III). Generated for this test; a tone, not speech,
#: so the assertion can be about pitch and length rather than about listening.
_TONE_MP3 = base64.b64decode(
    "//NkxAAVED7Ef0YYAim5dtttvd3d3bEAGAwGAwsmD5SCAIHB4Pg+/KHMuH+CG7l35cMcEz+JwQ1AmfyYIagG"
    "fyYIcBn+GOn3f/lwf7un+UAYPg+D4fBAEAQDCQfB8P4IBhVOkFuQCgUDAUDAYDgcBpSw27mnrEZQHcaKPTI7"
    "AeE5Ti2z5dU5sAjoDpA9fwdQ//NkxC4gQ4rKX5loAiFAewkv+FpCejuGGEu/8T0LSE9HcI0I1/+Yl0kTIvF5"
    "H//HqPUyLxJGJdLpl//+Xi8sumqkklooq////LxstFFST0f//////r1aKqS0TFSRktExMAJAIDAEQAIBABZg"
    "LQEaYDUAjmBGANJgyoeQYcoThGD0CKphmYsgfmzK0Gl7CvZh//NkxDAiikYQAd84AMKHOGEAg8hhD4JYYDcB"
    "cmAzAM5gNABiYAyAbGAOgBAsAHNN/f/2mIXN/tf0bH2/rvb7XWk36/fKl7/+vbUz7p/T7+ouqVLlPZRbn7+N"
    "rX5vSlsgab3svXlNFSqJqYAkCYggZ9acrcYGOC4mJ6jzZgyAByYLiKCnaOdBJjT4V2B1p1Aa//NkxCgbEkIU"
    "ANfqJDTeBkYdAYnEYGFgkBg0Fg2bC6gcZp715cdX9/1NnX1f/+p19X1ffOv/+rvz3q/6//WfstOv9/HKRR/+"
    "curxq/R27N9zqpAwdBQxIEzhA5bkwMgDKMVAD/DB5QMwVFeDYcfNow3IL0NKwkxQgjBJJAAoAIVAAEQUUm9l"
    "/////X/TDf6J//NkxD4WkIIYANf4JB/fqr5Xob+3uq/oydGx3v1V6f7PbuXXV0e3buvqfpqCoxYAEDgC8FAP"
    "hgWYAeYnCBAGDzAhZgjApiYOXwcmA7BY5m/8Y0/mDowMJyEOQnr3XC917prhdP7f2xn/s279y/1UWbP+t91a"
    "f9nu/+r9rG9//TVrrBk+RIAGAwBaYCCANGBd//NkxGYVWNYcAP7KaAE+YqUJCmEdguRhCot0bON4zGH7hhhv"
    "aQmdUoY8Kph0XGBAios0FWJ1bXT4A7//9tG//b9KUp9v9W//X+P///v4OvQv/2M/qX6PbT+66+rRQoCHr/v9"
    "9fuytsFfYy4FNQQDzouZBQxAwSTBa8Cyg3szx3vqW5fv+Q3/rd9VNP/6//8l//NkxJMYKj4YAP8EaGts/+n9"
    "jWf7/TSiTEFNRTMuMTAwqqqqqqqqqqqqqqqqqqpYVNIUAARGAIAgAvMARAfDAWAa8w14nFMHPBnjDMQ/A+Iy"
    "WeMiJCBj+zLN0mo0AOzJYYAxNFgmmWrc5Nr9sJ0//1bGv/b3/tr/b73Ud/+Xtr+yf/28a5b61/7WXW//Kabb"
    "//NkxLUPMD6Bvh54Ii/vf315TRVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVYEm6SAAgyALiIA4CoEeC"
    "AdowNEpqMEABTzDEAXE/J0elMkYAozg0TDO4KTIUDgUTJMK4GBwuYo/AWPtrEOv9v1bI///+y1/n/uo3/5v1"
    "G9af/38G6PlFf7bH2d2r107a//NkxOQZ2j4UAP8KaBh3u8d5ehVMMAbAC2fBQAoMBgATgCAhmA9g0JhrIfmY"
    "YeVUmMqk0pnkBn0fk5FhmmEmqJjfIWaYKaAmmCgA3xgmgDsYBoADGAZgE5gB4AmgkAgAAxSnp888DnoEiE/7"
    "evGdev2/VtJvp9tR3/5e/Hev9Lf+NHSNrz/vuj66Ojb89uXW//NkxOMZkj4UAP9EaKV6Kadmc3qKABgW6XXb"
    "f7C4ShsNAwgZNVYTHjcwoelV0wAZODWDEywxQ115ogsYWEmTAqjX+Bh0yUnEhs3CPMKRDVyp0pPSmHgLIlYH"
    "QMRMjOSAcKzCxLf1apcR7AgEaqtccKzLgcwonMmDwqScy/9F/0F54MA1gFYDGh8wEuDlYxMu//NkxP4gakIU"
    "AV8oADKQYwIt13H/2mIwSurAyNt1jqDBYFRIBgKhCFwJ+f////n8JHEJyHL1u8XuWuXuY8hjL0vWcJy/////"
    "/1+0/edw5Y/vyB2rjlRN2pe/sy/schn/06NKgoFQqNCQSHP/6PdvXUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVV//NkxP83qkbCX5vZIlVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAcAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
)


def _crossing_hz(pcm: bytes) -> float:
    s = array.array("h", pcm)
    mid = s[len(s) // 4 : len(s) * 3 // 4]
    ups = sum(1 for a, b in itertools.pairwise(mid) if a < 0 <= b)
    return ups / (len(mid) / eng.RATE)


def test_miniaudio_decodes_a_real_edge_format_mp3_to_bus_pcm() -> None:
    miniaudio = pytest.importorskip("miniaudio")
    pcm = eng._miniaudio_decode(_TONE_MP3, miniaudio)
    assert len(pcm) % 2 == 0
    # MP3 adds encoder delay and padding at both ends; it never shortens the clip.
    assert 250 <= eng.pcm_duration_ms(pcm) < 400
    assert abs(_crossing_hz(pcm) - 440.0) < 15
    assert max(abs(s) for s in array.array("h", pcm)) > 5000


def test_without_ffmpeg_the_edge_voice_decodes_through_miniaudio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no ffmpeg. This is the path that makes the British reader voice play there."""
    pytest.importorskip("miniaudio")
    monkeypatch.setattr(eng.shutil, "which", lambda name: None)
    engine = eng.EdgeEngine(fetch=lambda text, voice: bytearray(_TONE_MP3))
    pcm = engine.synth("Good evening.", "en")
    assert engine.voice_for("en") == "en-GB-RyanNeural"
    assert abs(_crossing_hz(pcm) - 440.0) < 15


def test_a_corrupt_mp3_is_a_failure_so_the_ladder_moves_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("miniaudio")
    monkeypatch.setattr(eng.shutil, "which", lambda name: None)
    with pytest.raises(eng.EngineFailed, match="miniaudio"):
        eng._mp3_decode(b"ID3 this is not audio" * 20)


def test_with_neither_decoder_the_refusal_names_both_fixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eng.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "miniaudio", None)
    with pytest.raises(eng.EngineUnavailable) as caught:
        eng._mp3_decode(_TONE_MP3)
    assert "ffmpeg" in str(caught.value) and "miniaudio" in str(caught.value)


def test_ffmpeg_still_goes_first_when_it_is_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []
    monkeypatch.setattr(eng.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(eng, "_ffmpeg_decode", lambda mp3: seen.append(mp3) or b"\x00\x00")
    assert eng._mp3_decode(b"mp3") == b"\x00\x00" and seen == [b"mp3"]
