"""`python -m jarvis window`: what the composition root hands the server, and where speech goes.

Speech is the interesting part. The desk owns the speaker while it runs (rule 2:
one output stream, or the echo canceller hears audio it was never shown), so the
window asks the desk to speak through a ``say`` command row; with no desk
running, it speaks itself. Which of the two happened is the whole test.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import __main__ as cli
from jarvis import kill, liveness, secrets
from jarvis.config import Config
from jarvis.db import connect, migrate


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(dbpath: Path) -> Iterator[sqlite3.Connection]:
    c = connect(dbpath)
    yield c
    c.close()


@pytest.fixture
def no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for s in secrets.SECRETS:
        monkeypatch.delenv(s.env, raising=False)
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


# ───────────────────────────── speech ─────────────────────────────


def test_with_the_desk_running_the_desk_is_asked_to_speak(
    dbpath: Path, con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    played: list[bytes] = []
    monkeypatch.setattr(cli, "_play", lambda pcm, rate, **kw: played.append(pcm))
    liveness.beat(con, "desk", state="awake")
    speak = cli._window_speaker(Config(), str(dbpath))
    assert speak("call mum") == "desk"
    (cmd,) = kill.pending_commands(con, actor="desk", verbs=("say",))
    assert cmd.args == {"text": "call mum"} and cmd.target_id == "desk"
    assert played == [], "a second output stream while the desk runs is echo"


def test_the_say_row_holds_no_secret_and_outlives_a_long_reply(
    dbpath: Path, con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.bus import Redactor

    liveness.beat(con, "desk", state="speaking")
    secret = "sk-" + "q" * 30
    speak = cli._window_speaker(Config(), str(dbpath), redactor=Redactor.of([secret]))
    assert speak(f"the key is {secret}") == "desk"
    (cmd,) = kill.pending_commands(con, actor="desk", verbs=("say",))
    assert secret not in cmd.args["text"]
    row = con.execute("SELECT expires_at, ts FROM commands WHERE id=?", (cmd.id,)).fetchone()
    assert row is not None
    from datetime import datetime

    left = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["ts"])
    # A desk mid-reply reads commands only when it stops talking.
    assert left.total_seconds() >= kill.DEFAULT_TTL_S


def test_the_window_shares_one_redactor_between_its_log_and_its_speech() -> None:
    import ast

    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "build_window_services"
    )
    src = ast.unparse(fn)
    assert src.count("desk_redactor()") == 1
    assert "_window_speaker(cfg, db_path, redactor=redactor, current=reload)" in src


def test_a_desk_that_said_goodbye_is_not_asked(
    dbpath: Path, con: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    liveness.gone(con, "desk")
    played: list[bytes] = []
    monkeypatch.setattr(cli, "_play", lambda pcm, rate, **kw: played.append(pcm) or None)
    monkeypatch.setattr(cli, "reader_engines", lambda cfg: [object()])

    class Speaker:
        def __init__(self, engines: Any) -> None:
            pass

        async def pcm_for(self, text: str, lang: str, *, exact: bool) -> bytes:
            assert exact is False
            return b"\x00\x01" * 10

    monkeypatch.setattr("jarvis.voice.verbatim.VerbatimSpeaker", Speaker)
    speak = cli._window_speaker(Config(), str(dbpath))
    assert speak("hello") == "local"
    assert played == [b"\x00\x01" * 10]
    assert kill.pending_commands(con, actor="desk", verbs=("say",)) == []


def test_no_desk_and_no_voice_is_a_sentence(dbpath: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "reader_engines", lambda cfg: [])
    speak = cli._window_speaker(Config(), str(dbpath))
    with pytest.raises(RuntimeError, match="no voice"):
        speak("hello")


def test_without_a_key_chat_is_off_and_says_why() -> None:
    chat, why = cli._window_chat(Config(), None, None, None, {}, "")
    assert chat is None and "secrets set gemini_api_key" in why


# ───────────────────────────── the command ─────────────────────────────


class FakeServer:
    def __init__(self) -> None:
        self.port = 54321
        self.token = "tok_" + "x" * 30
        self.url = f"http://127.0.0.1:{self.port}/#t={self.token}"
        self.shut = False

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def shutdown(self) -> None:
        self.shut = True


def run_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[dict[str, Any], list[str], FakeServer]:
    from jarvis.window import launch, server

    made: dict[str, Any] = {}
    opened: list[str] = []
    fake = FakeServer()

    def make_server(services: Any, **kw: Any) -> FakeServer:
        made["services"], made["kw"] = services, kw
        return fake

    monkeypatch.setattr(server, "make_server", make_server)
    monkeypatch.setattr(launch, "open_window", lambda url: opened.append(url) or "edge-app")
    assert cli.main(["--db", str(tmp_path / "w.db"), "window", *extra]) == 0
    return made, opened, fake


def test_the_window_command_builds_the_services_and_opens_the_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_secrets: None,
) -> None:
    made, opened, fake = run_window(tmp_path, monkeypatch)
    s = made["services"]
    assert len(s.registry) > 0 and callable(s.speak) and callable(s.open_db)
    assert s.chat is None and "gemini_api_key" in s.chat_why
    assert s.tz == Config().tz and s.redactor is not None
    assert made["kw"].get("port") == 0
    assert opened == [fake.url] and fake.shut
    out = capsys.readouterr().out
    assert "http://127.0.0.1:54321/" in out
    assert fake.token not in out, "the token is the whole access check; never print it unasked"


def test_no_open_prints_the_full_address_because_the_user_needs_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_secrets: None,
) -> None:
    _, opened, fake = run_window(tmp_path, monkeypatch, "--no-open", "--port", "8765")
    assert opened == []
    assert fake.url in capsys.readouterr().out


def test_a_busy_port_is_one_sentence_not_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_secrets: None,
) -> None:
    from jarvis.window import server

    def taken(services: Any, **kw: Any) -> Any:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(server, "make_server", taken)
    assert cli.main(["--db", str(tmp_path / "w.db"), "window", "--port", "8765"]) == 1
    err = capsys.readouterr().err
    assert "port 8765 is not free" in err and "Traceback" not in err


def test_every_request_gets_its_own_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_secrets: None
) -> None:
    made, _, _ = run_window(tmp_path, monkeypatch)
    a, b = made["services"].open_db(), made["services"].open_db()
    try:
        assert a is not b
        a.execute("SELECT 1 FROM events LIMIT 1")  # migrated before the first request
    finally:
        a.close()
        b.close()


def test_doctor_lists_the_window() -> None:
    import ast

    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    readiness = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_readiness"
    )
    assert "python -m jarvis window" in ast.unparse(readiness)


# ───────────────────────────── what the window speaks ─────────────────────────────


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Yarın İstanbul'da hava güneşli, efendim.", "tr"),
        ("Saat altıda annenizi arayın, efendim; unutmayın.", "tr"),
        ("Very good, sir. Eleven degrees and drizzling.", "en"),
        # ö and ü alone are German as often as Turkish: no guess.
        ("Schön, Herr Müller.", "en"),
        ("", "en"),
    ],
)
def test_the_reader_is_given_the_language_the_text_is_in(text: str, lang: str) -> None:
    from jarvis.voice.desk import language_of

    assert language_of(text) == lang


def test_the_local_reader_speaks_turkish_in_turkish(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    langs: list[str] = []
    monkeypatch.setattr(cli, "_play", lambda pcm, rate, **kw: None)
    monkeypatch.setattr(cli, "reader_engines", lambda cfg: [object()])

    class Speaker:
        def __init__(self, engines: Any) -> None:
            pass

        async def pcm_for(self, text: str, lang: str, *, exact: bool) -> bytes:
            langs.append(lang)
            return b"\x00\x00"

    monkeypatch.setattr("jarvis.voice.verbatim.VerbatimSpeaker", Speaker)
    speak = cli._window_speaker(Config(), str(dbpath))
    speak("Günaydın, efendim.")
    speak("Good morning, sir.")
    assert langs == ["tr", "en"]


def test_the_desk_reads_window_text_uncached_and_in_its_language() -> None:
    import ast

    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    desk = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_desk"
    )
    src = ast.unparse(desk)
    assert "replace(desk.reader, cache=None)" in src
    assert "lang=language_of(text)" in src


def test_window_speech_comes_out_of_the_speaker_the_desk_uses(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace as dc_replace

    devices: list[Any] = []
    monkeypatch.setattr(cli, "_play", lambda pcm, rate, **kw: devices.append(kw.get("device")))
    monkeypatch.setattr(cli, "reader_engines", lambda cfg: [object()])
    monkeypatch.setattr(cli, "_speaker_for", lambda cfg: 7 if cfg.voice.input_device else None)

    class Speaker:
        def __init__(self, engines: Any) -> None:
            pass

        async def pcm_for(self, text: str, lang: str, *, exact: bool) -> bytes:
            return b"\x00\x00"

    monkeypatch.setattr("jarvis.voice.verbatim.VerbatimSpeaker", Speaker)
    now = {"cfg": Config()}
    speak = cli._window_speaker(Config(), str(dbpath), current=lambda: now["cfg"])
    speak("hello")
    now["cfg"] = Config(voice=dc_replace(Config().voice, input_device="Jabra Evolve2 40"))
    speak("hello")
    assert devices == [None, 7], "a headset chosen in Settings is used at once"


def test_a_speaker_that_refuses_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    calls: list[Any] = []

    def play(samples: Any, rate: int, device: Any = None) -> None:
        calls.append(device)
        if device is not None:
            raise RuntimeError("Invalid sample rate [PaErrorCode -9997]")

    fake = types.SimpleNamespace(play=play, wait=lambda: None)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    assert cli._play(b"\x00\x00" * 10, 24000, device=3) is None
    assert calls == [3, None]
