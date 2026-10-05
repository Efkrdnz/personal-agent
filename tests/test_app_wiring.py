"""The app as the composition root wires it: callers exist, in the right order.

This tree's bugs live BETWEEN layers — a stream opened and never started, a
listener for events nobody emits. So besides the behaviour of each piece, these
tests assert that the CALLER exists: that ``app`` actually starts the
supervisor, publishes its address, opens the window and tears it all down;
that a desk refusal is written where the window reads it; that the user's
choice of address reaches the desk's instructions.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jarvis import __main__ as cli
from jarvis import secrets
from jarvis.app import adapters, autostart, instance, tray
from jarvis.app import supervisor as sup
from jarvis.config import Config, Persona
from jarvis.db import connect, migrate

ROOT = Path(cli.__file__)


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No real credentials, and every per-user directory under tmp_path."""
    for s in secrets.SECRETS:
        monkeypatch.delenv(s.env, raising=False)
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    monkeypatch.delenv("JARVIS_DB", raising=False)
    return tmp_path


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


def events(path: Path, kind: str) -> list[dict[str, Any]]:
    c = connect(path)
    try:
        rows = c.execute("SELECT payload FROM events WHERE kind=? ORDER BY seq", (kind,)).fetchall()
    finally:
        c.close()
    return [json.loads(r[0]) for r in rows]


# ───────────────────────────── the app, end to end, with fakes at the edges ──────────


class FakeSupervisor:
    made: list[FakeSupervisor] = []

    def __init__(self, specs: Any, **kw: Any) -> None:
        self.specs = tuple(specs)
        self.kw = kw
        self.started: list[str] = []
        self.restarted: list[str] = []
        self.stopped = False
        FakeSupervisor.made.append(self)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.specs)

    def start(self, name: str | None = None) -> None:
        self.started.append(name or "*")

    def stop(self, name: str | None = None, timeout: float = 5.0) -> None:
        self.stopped = True

    def restart(self, name: str) -> None:
        self.restarted.append(name)

    def poll(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {n: {"running": n in self.started, "held": False} for n in self.names}


class FakeServer:
    def __init__(self) -> None:
        self.port = 54321
        self.token = "tok_" + "x" * 30
        self.url = f"http://127.0.0.1:{self.port}/#t={self.token}"
        self.started = False
        self.shut = False

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shut = True


def run_app(
    monkeypatch: pytest.MonkeyPatch,
    dbpath: Path,
    *argv: str,
    during: Any = None,
) -> dict[str, Any]:
    """``python -m jarvis app`` with the server, children, window and tray faked."""
    from jarvis.window import launch, server

    seen: dict[str, Any] = {"opened": [], "tray": None}
    fake = FakeServer()
    FakeSupervisor.made.clear()

    def make_server(services: Any, **kw: Any) -> FakeServer:
        seen["services"], seen["server_kw"] = services, kw
        return fake

    def fake_tray(**kw: Any) -> str:
        seen["tray"] = kw
        # What the app looks like while the user is using it.
        seen["running"] = instance.running()
        seen["lock_free"] = instance.acquire() is not None
        if during is not None:
            during(kw, seen)
        kw["quit"]()
        assert kw["stop"].is_set()
        return "headless"

    monkeypatch.setattr(server, "make_server", make_server)
    monkeypatch.setattr(launch, "open_window", lambda url: seen["opened"].append(url) or "edge-app")
    monkeypatch.setattr(sup, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(tray, "run", fake_tray)
    seen["code"] = cli.main(["--db", str(dbpath), "app", *argv])
    seen["server"] = fake
    seen["supervisor"] = FakeSupervisor.made[-1] if FakeSupervisor.made else None
    return seen


def test_the_app_starts_everything_in_order_and_tears_it_all_down(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = run_app(monkeypatch, dbpath)
    s, fake = seen["supervisor"], seen["server"]
    assert seen["code"] == 0
    # The children: all three known, two started (no telegram token).
    assert [p.name for p in s.specs] == ["desk", "schedule", "telegram"]
    assert s.started == ["desk", "schedule"]
    assert s.kw["env"]["JARVIS_DB"] == str(dbpath.resolve())
    assert callable(s.kw["on_exit"])
    # The window: served on a free port, published, opened.
    assert seen["server_kw"] == {"port": 0} and fake.started
    assert seen["running"] is not None and seen["running"].port == fake.port
    assert seen["running"].token == fake.token
    assert seen["lock_free"] is False, "the lock is held while the app runs"
    assert seen["opened"] == [fake.url]
    # Torn down: children stopped, server shut, lock released.
    assert s.stopped and fake.shut
    lock = instance.acquire()
    assert lock is not None, "quitting must release the lock"
    lock.release()
    from jarvis import __version__

    assert events(dbpath, "app.started") == [{"version": __version__}]


def test_the_window_gets_the_controls_and_the_settings(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.app.setup import SetupService

    seen = run_app(monkeypatch, dbpath)
    services, s = seen["services"], seen["supervisor"]
    assert isinstance(services.control, sup.Control) and services.control.supervisor is s
    assert isinstance(services.setup, SetupService)
    services.control.restart("desk")
    assert s.restarted == ["desk"]
    # The first-run screen stores the key after the window is up, so chat is on
    # and builds itself later rather than being off until a restart.
    assert services.chat is not None and services.chat_why == ""
    status = services.setup.status()
    assert status["first_run"] is True and status["can"]["restart"] is True


def test_the_tray_opens_the_window_and_restarts_the_voice(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def during(kw: dict[str, Any], seen: dict[str, Any]) -> None:
        kw["open_window"]()
        kw["restart_voice"]()

    seen = run_app(monkeypatch, dbpath, during=during)
    assert seen["opened"] == [seen["server"].url] * 2
    assert seen["supervisor"].restarted == ["desk"]


def test_no_window_means_no_window(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = run_app(monkeypatch, dbpath, "--no-window")
    assert seen["opened"] == [] and seen["code"] == 0


def test_the_setting_to_not_open_the_window_is_obeyed(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jarvis.window import launch, server

    cfg = tmp_path / "config.toml"
    cfg.write_text("[app]\nopen_window_on_start = false\n", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(server, "make_server", lambda services, **kw: FakeServer())
    monkeypatch.setattr(launch, "open_window", lambda url: opened.append(url))
    monkeypatch.setattr(sup, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(tray, "run", lambda **kw: kw["quit"]())
    assert cli.main(["--db", str(dbpath), "--config", str(cfg), "app"]) == 0
    assert opened == []
    assert FakeSupervisor.made[-1].kw["env"]["JARVIS_CONFIG"] == str(cfg.resolve())


def test_a_broken_config_still_opens_the_window_that_explains_it(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[voice\nthis is not toml", encoding="utf-8")
    from jarvis.window import launch, server

    opened: list[str] = []
    monkeypatch.setattr(server, "make_server", lambda services, **kw: FakeServer())
    monkeypatch.setattr(launch, "open_window", lambda url: opened.append(url))
    monkeypatch.setattr(sup, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(tray, "run", lambda **kw: kw["quit"]())
    assert cli.main(["--db", str(dbpath), "--config", str(cfg), "app"]) == 0
    assert opened == [FakeServer().url]
    assert FakeSupervisor.made[-1].started == ["desk", "schedule"]


def test_telegram_starts_when_a_token_is_stored(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "123:abc")
    seen = run_app(monkeypatch, dbpath)
    assert seen["supervisor"].started == ["desk", "schedule", "telegram"]


def test_a_second_copy_opens_the_first_ones_window_and_starts_nothing(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.window import launch

    first = instance.acquire()
    assert first is not None
    try:
        instance.publish(4444, "t" * 40)
        opened: list[str] = []
        monkeypatch.setattr(launch, "open_window", lambda url: opened.append(url))
        monkeypatch.setattr(sup, "Supervisor", FakeSupervisor)
        FakeSupervisor.made.clear()
        assert cli.main(["--db", str(dbpath), "app"]) == 0
        assert opened == [f"http://127.0.0.1:4444/#t={'t' * 40}"]
        assert FakeSupervisor.made == []
    finally:
        first.release()


def test_ctrl_c_still_tears_down(home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(kw: dict[str, Any], seen: dict[str, Any]) -> None:
        raise KeyboardInterrupt

    seen = run_app(monkeypatch, dbpath, during=interrupted)
    assert seen["code"] == 0 and seen["supervisor"].stopped and seen["server"].shut


# ───────────────────────────── no command means the app ─────────────────────────────


def test_no_command_is_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(cli, "cmd_app", lambda args: ran.append(args) or 0)
    assert cli.main([]) == 0
    assert cli.main(["--db", "x.db"]) == 0
    assert [a.command for a in ran] == ["app", "app"] and ran[1].db == "x.db"
    assert ran[0].no_window is False and ran[0].selftest is False


def test_a_half_typed_option_is_the_real_parsers_to_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._with_default_command(["--db"]) == ["--db"]
    with pytest.raises(SystemExit):
        cli.main(["--db"])
    assert "python -m jarvis" in capsys.readouterr().err


def test_help_and_typos_still_name_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "doctor" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["dekс"])


# ───────────────────────────── the desk's refusals reach the window ─────────────────


def test_a_refusal_is_published_with_its_action_and_exits_2(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(args: Any) -> Any:
        raise cli.StartupRefused("wake model missing; download it", action="wake")

    monkeypatch.setattr(cli, "_build_desk", refuse)
    assert cli.main(["--db", str(dbpath), "desk"]) == 2
    assert events(dbpath, "desk.refused") == [
        {"sentence": "wake model missing; download it", "action": "wake"}
    ]
    assert "wake model missing" in capsys.readouterr().err, "the log still gets it"


def test_a_missing_key_names_the_secret(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setattr(cli, "_installed", lambda module: True)
    assert cli.main(["--db", str(dbpath), "desk"]) == 2
    (ev,) = events(dbpath, "desk.refused")
    assert ev["action"] == "secret:gemini_api_key" and "gemini_api_key" in ev["sentence"]


def test_every_refusal_site_says_which_button_fixes_it() -> None:
    tree = ast.parse(ROOT.read_text(encoding="utf-8"))
    raises = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "StartupRefused"
    ]
    assert len(raises) >= 6
    for call in raises:
        assert "action" in {k.arg for k in call.keywords}, ast.unparse(call)[:120]
    actions = {
        k.value.value
        for call in raises
        for k in call.keywords
        if k.arg == "action" and isinstance(k.value, ast.Constant)
    }
    assert actions == {None, "voice", "device", "wake"}


def test_the_desk_turns_every_refusal_into_an_event() -> None:
    desk = _fn("cmd_desk")
    assert sum(1 for n in ast.walk(desk) if _called(n) == "_refused") >= 4
    assert "publish" in _calls(_fn("_refused"))


# ───────────────────────────── the persona reaches every conversation ─────────────


def test_the_users_choice_of_address_reaches_the_desk(con: sqlite3.Connection) -> None:
    from jarvis.live import persona
    from jarvis.live.profiles import DESK, PHONE_USER

    withheld = cli.withheld_tools()
    able = {
        "builds": "code_build" not in withheld,
        "pc": "open_app" not in withheld,
        "commands": "run_command" not in withheld,
        "vision": "look_at_screen" not in withheld,
    }
    cfg = Config(persona=Persona(address="ma'am", name="Ada"))
    desk = cli.heard_profile(cfg, con, DESK)
    assert desk.system_instruction.startswith(persona.desk_instruction("ma'am", "Ada", **able))
    phone = cli.heard_profile(cfg, con, PHONE_USER)
    assert phone.system_instruction.startswith(persona.phone_instruction("ma'am", "Ada"))
    plain = cli.heard_profile(Config(), con, DESK)
    assert plain.system_instruction.startswith(persona.desk_instruction(**able))
    other = cli.heard_profile(cfg, con, replace(DESK, name="other", system_instruction="X"))
    assert other.system_instruction.startswith("X")


def test_both_chats_are_told_how_to_address_the_user() -> None:
    for name in ("cmd_chat", "_window_chat"):
        persona_calls = [n for n in ast.walk(_fn(name)) if _called(n) == "persona"]
        assert persona_calls, name
        for call in persona_calls:
            assert {"address", "name"} <= {k.arg for k in call.keywords}, name


def test_the_late_chat_waits_for_a_key_then_builds_once(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.live import chat as chatmod

    built: list[str] = []

    class FakeChat:
        def __init__(self, **kw: Any) -> None:
            built.append(kw["api_key"])

        def send(self, text: str) -> Any:
            from jarvis.live.chat import ChatTurn

            return ChatTurn(text=f"you said {text}")

    monkeypatch.setattr(chatmod, "GeminiChat", FakeChat)
    services = cli.build_window_services(
        Config(persona=Persona(name="Ada")), str(dbpath), late_key=True
    )
    with pytest.raises(RuntimeError, match="Settings"):
        services.chat("hello")
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "key-from-onboarding")
    assert services.chat("hello") == ("you said hello", ())
    assert services.chat("again")[0] == "you said again"
    assert built == ["key-from-onboarding"]


def test_the_plain_window_still_says_why_chat_is_off(home: Path, dbpath: Path) -> None:
    services = cli.build_window_services(Config(), str(dbpath))
    assert services.chat is None and "gemini_api_key" in services.chat_why
    assert services.control is None and services.setup is None


# ───────────────────────────── the callers exist ─────────────────────────────


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(ROOT.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _called(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ""
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _calls(fn: ast.FunctionDef) -> set[str]:
    return {_called(n) for n in ast.walk(fn)} - {""}


def test_the_app_command_wires_every_piece() -> None:
    assert {"acquire", "hand_off", "release", "main", "_run_app"} <= _calls(_fn("cmd_app"))
    run = _fn("_run_app")
    assert {
        "Supervisor",
        "app_specs",
        "exit_recorder",
        "Control",
        "build_window_services",
        "_setup_service",
        "make_server",
        "start",
        "publish",
        "sync",
        "boot_names",
        "open_window",
        "run",
        "stop",
        "shutdown",
    } <= _calls(run)
    names = {n.attr for n in ast.walk(run) if isinstance(n, ast.Attribute)}
    assert "poll_forever" in names, "nothing would ever reap a child or restart one"


def test_the_settings_service_gets_a_real_callable_for_everything() -> None:
    call = next(n for n in ast.walk(_fn("_setup_service")) if _called(n) == "SetupService")
    assert {
        "config_path",
        "db_path",
        "list_devices",
        "wake_ready",
        "download_wake",
        "preview_voice",
        "restart",
        "autostart",
        "claude_login",
        "control",
    } <= {k.arg for k in call.keywords}


def test_the_window_command_shares_the_services_builder() -> None:
    assert {"build_window_services", "make_server", "open_window"} <= _calls(_fn("cmd_window"))


def test_the_settings_service_built_here_works(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse

    s = sup.Supervisor((), log_path=lambda n: tmp_path / f"{n}.log")
    args = argparse.Namespace(config=None, db=str(dbpath))
    setup = cli._setup_service(Config(), args, s, sup.Control(s, threading.Event()))
    st = setup.status()
    # Start-with-Windows exists only on Windows; elsewhere the toggle is hidden.
    autostart = sys.platform == "win32"
    assert st["can"] == {"preview": True, "restart": True, "autostart": autostart, "claude": True}
    logins: list[Any] = []
    monkeypatch.setattr(adapters, "claude_login", lambda cli_path: logins.append(cli_path) or "ok")
    assert setup.sign_in_claude()["ok"] is True
    assert logins == [cli.claude_cli_path()]


# ───────────────────────────── adapters ─────────────────────────────


def test_no_portaudio_is_an_empty_device_list() -> None:
    from jarvis.audio.devices import AudioStackMissing

    class Missing:
        def devices(self) -> Any:
            raise AudioStackMissing("no usable PortAudio here")

    assert adapters.list_devices(probe=Missing()) == []


def test_devices_are_listed_by_the_name_the_desk_selects_by() -> None:
    from jarvis.audio.devices import DeviceInfo

    class Two:
        def devices(self) -> list[DeviceInfo]:
            return [
                DeviceInfo(0, "Microphone (Jabra Evolve2 40)", "Windows WASAPI", 1, 0, 48000.0),
                DeviceInfo(1, "Speakers (Jabra Evolve2 40)", "Windows WASAPI", 0, 2, 48000.0),
                DeviceInfo(2, "Microsoft Sound Mapper - Input", "MME", 2, 0, 44100.0),
            ]

    assert adapters.list_devices(probe=Two()) == [{"label": "Jabra Evolve2 40"}]


def test_a_tampered_wake_model_is_refused_with_a_sentence(tmp_path: Path) -> None:
    from jarvis.audio.wake import ModelsMissing

    with pytest.raises(ModelsMissing, match="refusing it"):
        adapters.download_wake("hey_jarvis", model_dir=tmp_path, fetch=lambda url: b"not a model")
    assert not any(tmp_path.glob("*.onnx")), "nothing half-installed"


def test_a_wake_model_already_present_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jarvis.audio import wake

    monkeypatch.setattr(wake, "download", lambda phrase, where, fetch=None: ())
    assert "already" in adapters.download_wake("hey_jarvis", model_dir=tmp_path)
    monkeypatch.setattr(wake, "download", lambda phrase, where, fetch=None: ("a.onnx",))
    assert "downloaded" in adapters.download_wake("hey_jarvis", model_dir=tmp_path)


def test_a_voice_sample_reads_the_key_when_it_is_asked_for() -> None:
    keys: list[str | None] = [None]
    played: list[tuple[bytes, int]] = []
    made: list[dict[str, Any]] = []

    class Engine:
        def __init__(self, **kw: Any) -> None:
            made.append(kw)

        def synth(self, text: str, lang: str) -> bytes:
            return b"\x01\x00" * 4

    preview = adapters.gemini_preview(
        key=lambda: keys[0],
        model="tts-model",
        play=lambda pcm, rate: played.append((pcm, rate)),
        engine=Engine,
    )
    with pytest.raises(RuntimeError, match="Gemini key"):
        preview("Charon", "Good evening, sir.")
    keys[0] = "k"
    preview("Charon", "Good evening, sir.")
    assert made == [{"api_key": "k", "model": "tts-model", "voice": "Charon"}]
    assert played == [(b"\x01\x00" * 4, 24_000)]


def test_a_sample_that_cannot_play_says_why() -> None:
    class Engine:
        def __init__(self, **kw: Any) -> None:
            pass

        def synth(self, text: str, lang: str) -> bytes:
            return b"\x00\x00"

    preview = adapters.gemini_preview(
        key=lambda: "k", model="m", play=lambda pcm, rate: "no output device", engine=Engine
    )
    with pytest.raises(RuntimeError, match="no output device"):
        preview("Puck", "hello")


def test_claude_sign_in_opens_its_own_console_on_windows() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    said = adapters.claude_login(
        r"C:\Jarvis\_internal\claude_agent_sdk\_bundled\claude.exe",
        platform="win32",
        popen=lambda argv, **kw: calls.append((argv, kw)),
    )
    ((argv, kw),) = calls
    assert argv == [r"C:\Jarvis\_internal\claude_agent_sdk\_bundled\claude.exe", "auth", "login"]
    assert kw["creationflags"] == adapters.CREATE_NEW_CONSOLE == 0x10
    assert "shell" not in kw and "sign-in" in said


def test_claude_sign_in_elsewhere_needs_a_terminal() -> None:
    calls: list[list[str]] = []
    adapters.claude_login(
        "/x/claude",
        platform="linux",
        popen=lambda argv, **kw: calls.append(argv),
        which=lambda name: "/usr/bin/x-terminal-emulator",
    )
    assert calls == [["/usr/bin/x-terminal-emulator", "-e", "/x/claude", "auth", "login"]]
    with pytest.raises(RuntimeError, match="no terminal"):
        adapters.claude_login("/x/claude", platform="linux", which=lambda name: None)
    with pytest.raises(RuntimeError, match="isn't installed"):
        adapters.claude_login(None, platform="win32")


def test_the_autostart_switch_exists_only_on_windows() -> None:
    assert adapters.autostart_switch("linux") is None
    assert adapters.autostart_switch("win32") is autostart.set_enabled


# ───────────────────────────── start with Windows ─────────────────────────────


class FakeRegistry:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def test_the_frozen_exe_starts_itself() -> None:
    exe = r"C:\Program Files\Jarvis\Jarvis.exe"
    assert autostart.command(frozen=True, executable=exe) == f'"{exe}"'


def test_a_source_install_starts_with_pythonw_never_a_console() -> None:
    exe = r"E:\personal-agent\.venv\Scripts\python.exe"
    got = autostart.command(frozen=False, executable=exe, exists=lambda p: True)
    assert got == r'"E:\personal-agent\.venv\Scripts\pythonw.exe" -m jarvis app'
    fallback = autostart.command(frozen=False, executable=exe, exists=lambda p: False)
    assert fallback == f'"{exe}" -m jarvis app'


def test_switching_it_on_and_off_writes_the_run_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "command", lambda: '"C:\\J\\Jarvis.exe"')
    reg = FakeRegistry()
    autostart.set_enabled(True, platform="win32", registry=reg)
    assert reg.values == {"Jarvis": '"C:\\J\\Jarvis.exe"'}
    assert autostart.is_enabled(platform="win32", registry=reg)
    autostart.set_enabled(False, platform="win32", registry=reg)
    assert reg.values == {} and not autostart.is_enabled(platform="win32", registry=reg)
    autostart.set_enabled(False, platform="win32", registry=reg)  # off twice is still off


def test_off_windows_nothing_is_touched() -> None:
    reg = FakeRegistry()
    said = autostart.set_enabled(True, platform="linux", registry=reg)
    assert reg.values == {} and "only available on Windows" in said
    autostart.sync(True, platform="linux", registry=reg)
    assert reg.values == {} and not autostart.is_enabled(platform="linux", registry=reg)


def test_a_moved_app_repoints_its_run_key_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "command", lambda: '"D:\\new\\Jarvis.exe"')
    reg = FakeRegistry(Jarvis='"C:\\old\\Jarvis.exe"')
    autostart.sync(True, platform="win32", registry=reg)
    assert reg.values == {"Jarvis": '"D:\\new\\Jarvis.exe"'}
    autostart.sync(False, platform="win32", registry=reg)
    assert reg.values == {}
    autostart.sync(False, platform="win32", registry=reg)
    assert reg.values == {}


# ───────────────────────────── the tray ─────────────────────────────


def test_without_a_display_pystray_is_simply_absent() -> None:
    # On a headless Linux box `import pystray` raises an Xlib error, not
    # ImportError. Windows and macOS always have a tray to draw in.
    if sys.platform != "linux" or os.environ.get("DISPLAY"):
        pytest.skip("a display is present")
    assert tray.load_pystray() is None


def test_no_tray_blocks_on_the_event_until_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tray, "load_pystray", lambda: None)
    stop = threading.Event()
    out: list[str] = []
    t = threading.Thread(
        target=lambda: out.append(
            tray.run(
                open_window=lambda: None,
                restart_voice=lambda: None,
                quit=stop.set,
                stop=stop,
                poll_s=0.01,
            )
        )
    )
    t.start()
    t.join(0.1)
    assert t.is_alive(), "with no tray the app must keep running"
    stop.set()
    t.join(5)
    assert out == ["headless"]


class FakeTray:
    """Just enough of pystray: a menu, and an icon whose run() blocks until stop()."""

    class Menu(tuple):
        SEPARATOR = ("-", None, False)

        def __new__(cls, *items: Any) -> FakeTray.Menu:
            return super().__new__(cls, items)

    @staticmethod
    def MenuItem(text: str, action: Any, default: bool = False) -> Any:  # noqa: N802 - pystray's
        return (text, action, default)

    def __init__(self, fail_run: bool = False) -> None:
        self.icon: Any = None
        self.fail_run = fail_run

    def Icon(self, name: str, image: Any, title: str, menu: Any) -> Any:  # noqa: N802
        outer = self

        class Icon:
            def __init__(self) -> None:
                self.image, self.title, self.menu = image, title, menu
                self.done = threading.Event()

            def run(self) -> None:
                if outer.fail_run:
                    raise OSError("no notification area")
                self.done.wait(10)

            def stop(self) -> None:
                self.done.set()

        self.icon = Icon()
        return self.icon


def test_the_tray_menu_and_its_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL")
    fake = FakeTray()
    stop = threading.Event()
    did: list[str] = []

    def press_everything() -> None:
        while fake.icon is None:
            threading.Event().wait(0.01)
        items = [i for i in fake.icon.menu if i[1] is not None]
        assert [i[0] for i in items] == ["Open Jarvis", "Restart voice", "Quit"]
        assert items[0][2] is True, "a click on the icon opens the window"
        for _, action, _ in items:
            action(fake.icon, None)

    presser = threading.Thread(target=press_everything)
    presser.start()
    got = tray.run(
        open_window=lambda: did.append("open"),
        restart_voice=lambda: did.append("restart"),
        quit=lambda: (did.append("quit"), stop.set()),
        stop=stop,
        pystray=fake,
    )
    presser.join(5)
    assert got == "tray" and did == ["open", "restart", "quit"]
    assert fake.icon.title == "Jarvis" and fake.icon.image.size == (64, 64)


def test_the_hud_quit_also_closes_the_tray() -> None:
    pytest.importorskip("PIL")
    fake = FakeTray()
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    got = tray.run(
        open_window=lambda: None, restart_voice=lambda: None, quit=stop.set, stop=stop, pystray=fake
    )
    assert got == "tray"


def test_a_tray_that_cannot_run_falls_back_to_waiting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("PIL")
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    got = tray.run(
        open_window=lambda: None,
        restart_voice=lambda: None,
        quit=stop.set,
        stop=stop,
        pystray=FakeTray(fail_run=True),
        poll_s=0.01,
    )
    assert got == "headless" and "no notification area" in capsys.readouterr().err


def test_a_failing_menu_action_is_logged_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("PIL")
    fake = FakeTray()
    stop = threading.Event()

    def boom() -> None:
        raise RuntimeError("no browser")

    def press() -> None:
        while fake.icon is None:
            threading.Event().wait(0.01)
        fake.icon.menu[0][1](fake.icon, None)
        stop.set()

    threading.Thread(target=press).start()
    tray.run(open_window=boom, restart_voice=lambda: None, quit=stop.set, stop=stop, pystray=fake)
    assert "no browser" in capsys.readouterr().err


def test_the_icon_is_an_arc_reactor_at_every_size() -> None:
    pytest.importorskip("PIL")
    for size in (16, 32, 64, 256):
        img = tray.icon_image(size)
        assert img.size == (size, size) and img.mode == "RGBA"
    img = tray.icon_image(64)
    assert img.getpixel((0, 0))[3] == 0, "the corners are transparent"
    r, g, b, a = img.getpixel((32, 32))
    assert a == 255 and min(r, g, b) > 200, "the core is bright"


def test_the_app_chat_follows_the_key_and_the_settings_as_they_change(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Onboarding stores the key after the window is up; Settings then change
    # the city and the form of address. A chat built once would answer with
    # what was true at launch until the user restarted the app.
    from jarvis.live import chat as chatmod

    built: list[tuple[str, str]] = []

    class FakeChat:
        def __init__(self, **kw: Any) -> None:
            built.append((kw["api_key"], kw["system_instruction"]))

        def send(self, text: str) -> Any:
            from jarvis.live.chat import ChatTurn

            return ChatTurn(text="very good")

    monkeypatch.setattr(chatmod, "GeminiChat", FakeChat)
    current = {"cfg": Config(persona=Persona(address="sir"))}
    services = cli.build_window_services(
        current["cfg"], str(dbpath), late_key=True, reload=lambda: current["cfg"]
    )
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "first-key")
    services.chat("hello")
    services.chat("again")
    assert [k for k, _ in built] == ["first-key"], "unchanged settings keep the conversation"
    assert "search" in dict(services.extra), "a key stored after launch turns web search on"

    current["cfg"] = Config(persona=Persona(address="ma'am"))
    services.chat("hello")
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "replaced-key")
    services.chat("hello")
    assert [k for k, _ in built] == ["first-key", "first-key", "replaced-key"]
    assert "ma'am" in built[1][1] and "sir" in built[0][1]


def test_the_app_passes_the_config_as_it_is_now() -> None:
    call = next(
        n
        for n in ast.walk(_fn("_run_app"))
        if isinstance(n, ast.Call) and _called(n) == "build_window_services"
    )
    assert {"late_key", "reload"} <= {k.arg for k in call.keywords}


# ───────────────────────────── no promise the app cannot keep ─────────────────────────────


def test_the_app_offers_no_build_it_would_never_carry_out(
    home: Path, dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing in the app advances a build request: offered, "build me X" was
    # filed, promised, never started, and then blocked every later build.
    # Whether this machine can be driven is its own question (a headless CI box
    # cannot), pinned here so this test is only about the app.
    assert cli.withheld_tools(in_app=True, pc_available=True) == ("code_build",)
    assert cli.withheld_tools(in_app=False, pc_available=True) == ()
    monkeypatch.setenv("JARVIS_APP", "1")
    assert cli.withheld_tools(pc_available=True) == ("code_build",)

    app_window = cli.build_window_services(Config(), str(dbpath), late_key=True)
    assert "code_build" not in app_window.registry.names("cli")
    plain = cli.build_window_services(Config(), str(dbpath))
    assert "code_build" in plain.registry.names("cli"), "a terminal user can still `jarvis build`"


def test_the_desk_in_the_app_says_it_cannot_build(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.live.profiles import DESK

    con = connect(dbpath)
    try:
        monkeypatch.setenv("JARVIS_APP", "1")
        in_app = cli.heard_profile(Config(), con, DESK).system_instruction
        monkeypatch.delenv("JARVIS_APP")
        outside = cli.heard_profile(Config(), con, DESK).system_instruction
    finally:
        con.close()
    assert "drive Claude Code" not in in_app and "not available from here" in in_app
    assert "drive Claude Code" in outside


def test_the_app_children_are_told_they_are_in_the_app() -> None:
    # withheld_tools() reads JARVIS_APP; the supervisor is what sets it.
    src = (ROOT.parent / "app" / "supervisor.py").read_text(encoding="utf-8")
    assert '"JARVIS_APP": "1"' in src


def test_no_app_process_advances_a_build_yet() -> None:
    # The day one does, the build tool can be offered in the app again:
    # withheld_tools() must change with this.
    assert {s.name for s in sup.app_specs()} == {"desk", "schedule", "telegram"}
