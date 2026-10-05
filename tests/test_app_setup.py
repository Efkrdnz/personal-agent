"""The window's setup service, with a fake for every collaborator and a real temp config.

Three promises are tested hardest, because each failure is invisible until it
matters: a credential never comes back out (not in a reply, not in an
exception, not in a log), a setting goes in only through config's type-checked
writer, and nothing the window shows names a terminal command.
"""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from jarvis import config as cfgmod
from jarvis import liveness
from jarvis import secrets as real_secrets
from jarvis.app import setup as setup_mod
from jarvis.app.setup import SECRET_NAMES, SetupService, find_problems, restarts_for
from jarvis.bus import publish
from jarvis.db import connect, migrate

KEY = "AIzaSyD-THIS-IS-THE-SECRET-VALUE-0123456789"


class FakeSecrets:
    """The jarvis.secrets surface the service uses: get, store, keyring_available."""

    def __init__(self, stored: dict[str, str] | None = None) -> None:
        self.stored: dict[str, str] = dict(stored or {})
        self.fail_with: str | None = None
        self.backend_ok = True

    def get(self, name: str) -> str | None:
        return self.stored.get(name)

    def store(self, name: str, value: str) -> None:
        if self.fail_with is not None:
            # A backend free to quote what it was given, as real ones are.
            raise RuntimeError(self.fail_with.format(value=value))
        self.stored[name] = value

    def keyring_available(self) -> tuple[bool, str]:
        if self.backend_ok:
            return True, "keyring backend: Fake"
        return False, (
            "no usable keyring backend. On Linux the login keyring is unlocked by PAM. "
            'Run pip install -e ".[secrets]"'
        )


class Fakes:
    """Every injected callable, recording what it was asked."""

    def __init__(self) -> None:
        self.devices: list[dict[str, Any]] = [{"label": "Jabra Evolve2 40"}, {"label": "Realtek"}]
        self.devices_error: Exception | None = None
        self.ready: dict[str, bool] = {"hey_jarvis": False}
        self.downloads: list[str] = []
        self.download_error: Exception | None = None
        self.previews: list[tuple[str, str]] = []
        self.preview_error: Exception | None = None
        self.restarts: list[str] = []
        self.restart_refuses: set[str] = set()
        self.autostarts: list[bool] = []
        self.autostart_error: Exception | None = None
        self.logins = 0
        self.login_error: Exception | None = None

    def list_devices(self) -> list[dict[str, Any]]:
        if self.devices_error is not None:
            raise self.devices_error
        return self.devices

    def wake_ready(self, word: str) -> bool:
        return self.ready.get(word, False)

    def download_wake(self, word: str) -> str:
        if self.download_error is not None:
            raise self.download_error
        self.downloads.append(word)
        self.ready[word] = True
        return "Fetched 3 files into the models folder."

    def preview_voice(self, voice: str, sentence: str) -> None:
        if self.preview_error is not None:
            raise self.preview_error
        self.previews.append((voice, sentence))

    def restart(self, name: str) -> None:
        if name in self.restart_refuses:
            raise ValueError(f"There is no process called {name!r} to restart.")
        self.restarts.append(name)

    def autostart(self, on: bool) -> None:
        if self.autostart_error is not None:
            raise self.autostart_error
        self.autostarts.append(on)

    def claude_login(self) -> str:
        if self.login_error is not None:
            raise self.login_error
        self.logins += 1
        return "The Claude Code sign-in is open in your browser."


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d / "config.toml"


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "j.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(dbpath: Path) -> Iterator[Any]:
    c = connect(dbpath)
    yield c
    c.close()


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def keys() -> FakeSecrets:
    return FakeSecrets()


Build = Callable[..., SetupService]


@pytest.fixture
def make(cfg_path: Path, dbpath: Path, fakes: Fakes, keys: FakeSecrets) -> Build:
    def build(**over: Any) -> SetupService:
        kw: dict[str, Any] = {
            "config_path": cfg_path,
            "db_path": dbpath,
            "secrets_mod": keys,
            "list_devices": fakes.list_devices,
            "wake_ready": fakes.wake_ready,
            "download_wake": fakes.download_wake,
            "preview_voice": fakes.preview_voice,
            "restart": fakes.restart,
            "autostart": fakes.autostart,
            "claude_login": fakes.claude_login,
            # Synchronous, so a test sees the sample's effects when preview returns.
            "run_later": lambda fn: fn(),
            "clock": lambda: datetime(2026, 10, 5, 20, 15),
        }
        kw.update(over)
        return SetupService(**kw)

    return build


@pytest.fixture
def svc(make: Build) -> SetupService:
    return make()


def _no_command(obj: Any) -> None:
    text = json.dumps(obj)
    # Naming config.toml is fine (a broken one has to be named); TELLING
    # somebody to run something is not.
    for marker in ("python -m", "pip install", "secrets set", "wake download", "sudo "):
        assert marker not in text, f"{marker!r} reached the window: {text}"


# ───────────────────────────── construction ─────────────────────────────


def test_the_constructor_takes_the_contracts_keywords() -> None:
    # The composition root builds this from the contract's text; a renamed
    # keyword is a TypeError at app start that no other test would see.
    params = inspect.signature(SetupService).parameters
    for name in (
        "config_path",
        "db_path",
        "secrets_mod",
        "list_devices",
        "wake_ready",
        "download_wake",
        "preview_voice",
        "restart",
        "autostart",
        "claude_login",
    ):
        assert name in params, name
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["secrets_mod"].default is real_secrets


def test_the_secrets_it_offers_are_real_secrets() -> None:
    known = {s.name for s in real_secrets.SECRETS}
    assert set(SECRET_NAMES) <= known
    assert "google_oauth_client" not in SECRET_NAMES


# ───────────────────────────── status ─────────────────────────────


def test_a_fresh_install_is_a_first_run(svc: SetupService, fakes: Fakes) -> None:
    s = svc.status()
    assert s["first_run"] is True
    assert s["secrets"] == dict.fromkeys(SECRET_NAMES, False)
    assert s["wake"]["word"] == "hey_jarvis"
    assert s["wake"]["phrase"] == "hey jarvis"
    assert s["wake"]["ready"] is False
    assert "non-commercial" in s["wake"]["licence"]
    assert s["devices"] == [
        {"label": "Jabra Evolve2 40", "selected": False},
        {"label": "Realtek", "selected": False},
    ]
    assert s["problems"] == []
    assert s["can"] == {"preview": True, "restart": True, "autostart": True, "claude": True}


def test_status_has_every_settable_key_with_json_values(svc: SetupService) -> None:
    settings = svc.status()["settings"]
    assert set(settings) == set(cfgmod.SETTABLE)
    assert settings["persona.address"] == "sir"
    assert settings["voice.gemini_voice"] == "Charon"
    assert settings["voice.reader_voice"] == "en-GB-RyanNeural"
    assert settings["voice.input_device"] == ""  # None, as the page can show it
    assert settings["voice.vocabulary"] == []
    assert settings["app.start_with_windows"] is False
    json.dumps(settings)


def test_choices_hold_the_voices_and_the_current_value(svc: SetupService, cfg_path: Path) -> None:
    c = svc.status()["choices"]
    assert c["voice.gemini_voice"][0] == "Charon" or "Charon" in c["voice.gemini_voice"]
    assert c["voice.reader_voice"] == [
        "en-GB-RyanNeural",
        "en-GB-ThomasNeural",
        "en-GB-SoniaNeural",
        "en-US-GuyNeural",
    ]
    assert c["persona.address"] == ["sir", "ma'am", "boss"]
    assert c["location.units"] == ["metric", "imperial"]
    assert "" in c["voice.wake_word"] and "hey_jarvis" in c["voice.wake_word"]
    # A voice set by hand in config.toml stays selectable.
    cfg_path.write_text('[voice]\nreader_voice = "en-IE-ConnorNeural"\n', encoding="utf-8")
    assert svc.status()["choices"]["voice.reader_voice"][0] == "en-IE-ConnorNeural"


def test_the_chosen_microphone_is_marked(make: Build, cfg_path: Path) -> None:
    cfgmod.save_setting("voice.input_device", "Realtek", cfg_path)
    devices = make().status()["devices"]
    assert [d["selected"] for d in devices] == [False, True]


def test_a_stored_key_or_saved_setting_ends_the_first_run(
    make: Build, keys: FakeSecrets, cfg_path: Path
) -> None:
    keys.stored["gemini_api_key"] = KEY
    s = make().status()
    assert s["first_run"] is False
    assert s["secrets"]["gemini_api_key"] is True
    assert KEY not in json.dumps(s)
    keys.stored.clear()
    cfgmod.save_setting("persona.name", "Efe", cfg_path)
    assert make().status()["first_run"] is False


def test_a_keyring_that_throws_reads_as_absent(make: Build) -> None:
    class Broken(FakeSecrets):
        def get(self, name: str) -> str | None:
            raise RuntimeError("D-Bus is not running")

    assert make(secrets_mod=Broken()).status()["secrets"] == dict.fromkeys(SECRET_NAMES, False)


def test_no_microphone_list_is_a_sentence_not_a_crash(make: Build, fakes: Fakes) -> None:
    fakes.devices_error = OSError("PortAudio library not found")
    s = make().status()
    assert s["devices"] == []
    assert s["devices_why"] == "PortAudio library not found."


def test_a_broken_config_shows_defaults_and_says_so(svc: SetupService, cfg_path: Path) -> None:
    cfg_path.write_text('[telegram]\ntoken = "123:abc"\n', encoding="utf-8")
    s = svc.status()
    assert s["settings"]["persona.address"] == "sir"
    (problem,) = [p for p in s["problems"] if p["process"] == "config"]
    assert "credential" in problem["sentence"]
    _no_command(s)


def test_no_wake_word_needs_no_model(make: Build, cfg_path: Path) -> None:
    cfgmod.save_setting("voice.wake_word", "", cfg_path)
    assert make().status()["wake"] == {
        "word": "",
        "phrase": "",
        "ready": True,
        "licence": make().status()["wake"]["licence"],
    }


# ───────────────────────────── secrets ─────────────────────────────


def test_a_key_is_stored_and_never_echoed(
    svc: SetupService, keys: FakeSecrets, fakes: Fakes
) -> None:
    reply = svc.set_secret("gemini_api_key", KEY)
    assert keys.stored["gemini_api_key"] == KEY
    assert reply == {"ok": True, "present": True, "restarted": ["desk"]}
    assert KEY not in json.dumps(reply)
    assert fakes.restarts == ["desk"]


@pytest.mark.parametrize(
    ("name", "restarted"),
    [
        ("gemini_api_key", ["desk"]),
        ("telegram_bot_token", ["telegram"]),
        ("github_token", []),
        ("maxmind_license_key", []),
    ],
)
def test_each_key_restarts_what_reads_it(
    svc: SetupService, fakes: Fakes, name: str, restarted: list[str]
) -> None:
    assert svc.set_secret(name, "abc123")["restarted"] == restarted
    assert fakes.restarts == restarted


def test_onboarding_can_store_without_restarting(svc: SetupService, fakes: Fakes) -> None:
    assert svc.set_secret("gemini_api_key", KEY, restart=False)["restarted"] == []
    assert fakes.restarts == []


def test_a_process_the_app_does_not_run_is_not_an_error(
    svc: SetupService, fakes: Fakes, keys: FakeSecrets
) -> None:
    fakes.restart_refuses = {"telegram"}
    assert svc.set_secret("telegram_bot_token", "123:abc")["restarted"] == []
    assert keys.stored["telegram_bot_token"] == "123:abc"


def test_no_restart_callable_restarts_nothing(make: Build) -> None:
    assert make(restart=None).set_secret("gemini_api_key", KEY)["restarted"] == []


@pytest.mark.parametrize("name", ["google_oauth_client", "nope", "", "GEMINI_API_KEY"])
def test_only_the_four_keys_are_stored(svc: SetupService, keys: FakeSecrets, name: str) -> None:
    with pytest.raises(ValueError, match="store"):
        svc.set_secret(name, KEY)
    assert keys.stored == {}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "two words",
        "line\nbreak",
        "tab\there",
        " leading",
        "x" * 4097,
        "é" * 2049,  # 4098 bytes in UTF-8
        "bell\x07",
        None,
        12345,
    ],
)
def test_a_value_that_is_not_a_key_is_refused_without_quoting_it(
    svc: SetupService, keys: FakeSecrets, value: Any
) -> None:
    with pytest.raises(ValueError) as exc:
        svc.set_secret("gemini_api_key", value)
    assert keys.stored == {}
    if isinstance(value, str) and len(value) > 3:
        assert value.strip() not in str(exc.value)


def test_a_keyring_failure_never_carries_the_value(svc: SetupService, keys: FakeSecrets) -> None:
    keys.fail_with = "backend rejected password {value!r}"
    keys.backend_ok = False
    with pytest.raises(RuntimeError) as exc:
        svc.set_secret("gemini_api_key", KEY)
    assert KEY not in str(exc.value)
    assert "keyring" in str(exc.value)
    assert "pip install" not in str(exc.value)
    # Nothing chained: a printed traceback would show the backend's message.
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True


def test_a_keyring_failure_with_a_working_backend_names_only_the_type(
    svc: SetupService, keys: FakeSecrets
) -> None:
    keys.fail_with = "nope {value}"
    with pytest.raises(RuntimeError, match=r"\(RuntimeError\)") as exc:
        svc.set_secret("telegram_bot_token", KEY)
    assert KEY not in str(exc.value)


# ───────────────────────────── settings ─────────────────────────────


def test_a_setting_goes_through_config_and_restarts_the_desk(
    svc: SetupService, fakes: Fakes, cfg_path: Path
) -> None:
    reply = svc.set_setting("persona.address", "boss")
    assert reply == {"ok": True, "restarted": ["desk"], "note": ""}
    assert cfgmod.load(cfg_path).persona.address == "boss"
    assert cfgmod.settings_path(cfg_path).exists()
    assert not cfg_path.exists(), "config.toml is never written by the app"


@pytest.mark.parametrize(
    ("key", "processes"),
    [
        ("voice.gemini_voice", ("desk",)),
        ("voice.input_device", ("desk",)),
        ("voice.wake_word", ("desk",)),
        ("persona.name", ("desk",)),
        ("location.city", ("desk",)),
        ("tz", ("desk", "schedule")),
        ("app.start_telegram", ()),
        ("app.start_with_windows", ()),
        ("app.open_window_on_start", ()),
    ],
)
def test_restarts_for_each_key(key: str, processes: tuple[str, ...]) -> None:
    assert restarts_for(key) == processes


def test_every_settable_key_has_a_considered_restart() -> None:
    # A new key must be placed deliberately: it either restarts something or
    # is one the app reads at its own start.
    for key in cfgmod.SETTABLE:
        assert restarts_for(key) or key.startswith("app."), key


def test_app_switches_say_when_they_apply(svc: SetupService, fakes: Fakes) -> None:
    reply = svc.set_setting("app.start_telegram", False)
    assert reply["restarted"] == [] and "next time" in reply["note"]
    assert fakes.restarts == []


def test_onboarding_can_save_without_restarting(svc: SetupService, fakes: Fakes) -> None:
    reply = svc.set_setting("voice.input_device", "Jabra Evolve2 40", restart=False)
    assert reply["restarted"] == []
    assert fakes.restarts == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("desk.permission_mode", "dontAsk"),
        ("voice.model", "x"),
        ("app.start_telegram", "false"),
        ("voice.gemini_voice", "Nobody"),
        ("voice.gemini_voice", 3),
        ("voice.wake_word", "ok_computer"),
        ("location.units", "furlongs"),
    ],
)
def test_bad_settings_are_refused_and_nothing_restarts(
    svc: SetupService, fakes: Fakes, cfg_path: Path, key: str, value: Any
) -> None:
    with pytest.raises(ValueError):
        svc.set_setting(key, value)
    assert fakes.restarts == []
    assert not cfgmod.settings_path(cfg_path).exists()


def test_a_credential_typed_into_a_setting_is_refused(svc: SetupService, cfg_path: Path) -> None:
    with pytest.raises(cfgmod.SecretInConfig) as exc:
        svc.set_setting("location.city", KEY)
    assert KEY not in str(exc.value)
    assert not cfgmod.settings_path(cfg_path).exists()


def test_start_with_windows_switches_autostart_and_saves(
    svc: SetupService, fakes: Fakes, cfg_path: Path
) -> None:
    svc.set_setting("app.start_with_windows", True)
    assert fakes.autostarts == [True]
    assert cfgmod.load(cfg_path).app.start_with_windows is True
    svc.set_setting("app.start_with_windows", False)
    assert fakes.autostarts == [True, False]


def test_a_failed_autostart_leaves_the_setting_as_it_was(
    svc: SetupService, fakes: Fakes, cfg_path: Path
) -> None:
    fakes.autostart_error = PermissionError("registry is read-only")
    with pytest.raises(RuntimeError, match="(?i)registry is read-only"):
        svc.set_setting("app.start_with_windows", True)
    assert cfgmod.load(cfg_path).app.start_with_windows is False


def test_without_autostart_turning_it_on_is_refused_and_off_is_fine(
    make: Build, cfg_path: Path
) -> None:
    svc = make(autostart=None)
    with pytest.raises(RuntimeError, match="isn't available"):
        svc.set_setting("app.start_with_windows", True)
    assert not cfgmod.settings_path(cfg_path).exists()
    svc.set_setting("app.start_with_windows", False)
    assert cfgmod.load(cfg_path).app.start_with_windows is False


def test_autostart_refuses_a_string(svc: SetupService, fakes: Fakes) -> None:
    with pytest.raises(ValueError):
        svc.set_setting("app.start_with_windows", "true")
    assert fakes.autostarts == []


def test_two_saves_at_once_both_land(svc: SetupService, cfg_path: Path) -> None:
    # Each save rewrites the whole overlay; without the service's lock, two
    # clicks at once would each write a file missing the other's key.
    keys = [
        ("persona.name", "Efe"),
        ("location.city", "İzmir"),
        ("persona.address", "boss"),
        ("location.units", "imperial"),
        ("voice.reader_voice", "en-GB-ThomasNeural"),
        ("tz", "Europe/London"),
    ]
    start = threading.Barrier(len(keys))

    def save(k: str, v: str) -> None:
        start.wait()
        svc.set_setting(k, v, restart=False)

    threads = [threading.Thread(target=save, args=kv) for kv in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cfg = cfgmod.load(cfg_path)
    assert (cfg.persona.name, cfg.location.city, cfg.persona.address) == ("Efe", "İzmir", "boss")
    assert (cfg.location.units, cfg.voice.reader_voice, cfg.tz) == (
        "imperial",
        "en-GB-ThomasNeural",
        "Europe/London",
    )


# ───────────────────────────── wake model ─────────────────────────────


def test_the_wake_model_downloads_then_the_desk_restarts(svc: SetupService, fakes: Fakes) -> None:
    reply = svc.download_wake()
    assert fakes.downloads == ["hey_jarvis"]
    assert reply["ok"] is True and reply["ready"] is True
    assert reply["message"] == "Fetched 3 files into the models folder."
    assert "non-commercial" in reply["licence"]
    assert reply["restarted"] == ["desk"]


def test_a_model_already_here_is_not_fetched_again(svc: SetupService, fakes: Fakes) -> None:
    fakes.ready["hey_jarvis"] = True
    reply = svc.download_wake()
    assert fakes.downloads == [] and fakes.restarts == []
    assert "already" in reply["message"]


def test_a_model_already_here_still_wakes_a_desk_held_waiting_for_it(
    make: Build, fakes: Fakes
) -> None:
    # The desk restarted before the background download finished and is held
    # on "no wake model"; the card's button is how the user wakes it.
    class Control:
        def status(self) -> dict[str, Any]:
            return {"desk": {"running": False, "held": True, "reason": "no wake model"}}

    fakes.ready["hey_jarvis"] = True
    reply = make(control=Control()).download_wake()
    assert fakes.downloads == [] and reply["restarted"] == ["desk"]


def test_no_wake_word_downloads_nothing(svc: SetupService, fakes: Fakes, cfg_path: Path) -> None:
    cfgmod.save_setting("voice.wake_word", "", cfg_path)
    assert "listens all the time" in svc.download_wake()["message"]
    assert fakes.downloads == []


def test_a_failed_download_is_a_sentence_without_a_command(svc: SetupService, fakes: Fakes) -> None:
    fakes.download_error = OSError(
        "connection reset. Try again, or run python -m jarvis wake download"
    )
    with pytest.raises(RuntimeError) as exc:
        svc.download_wake()
    assert "connection reset" in str(exc.value).lower()
    assert "python -m" not in str(exc.value)
    assert fakes.restarts == []


def test_onboarding_downloads_without_restarting(svc: SetupService, fakes: Fakes) -> None:
    assert svc.download_wake(restart=False)["restarted"] == []
    assert fakes.downloads == ["hey_jarvis"] and fakes.restarts == []


# ───────────────────────────── voice samples ─────────────────────────────


def test_a_sample_plays_in_jarvis_manner_with_the_users_address(
    svc: SetupService, fakes: Fakes, cfg_path: Path
) -> None:
    cfgmod.save_setting("persona.address", "boss", cfg_path)
    assert svc.preview("Charon") == {"ok": True, "message": "Playing Charon."}
    ((voice, sentence),) = fakes.previews
    assert voice == "Charon"
    assert sentence == "Good evening, boss. This is Charon. Shall I carry on in this voice?"
    assert "!" not in sentence


@pytest.mark.parametrize("hour", [7, 13, 21])
def test_the_greeting_follows_the_clock(make: Build, hour: int) -> None:
    line = make(clock=lambda: datetime(2026, 1, 1, hour)).sample_line("Charon")
    assert line.startswith({7: "Good morning", 13: "Good afternoon", 21: "Good evening"}[hour])


def test_a_sample_of_an_unknown_voice_is_refused(svc: SetupService, fakes: Fakes) -> None:
    for bad in ("Nobody", "", 3, None):
        with pytest.raises(ValueError):
            svc.preview(bad)  # type: ignore[arg-type]
    assert fakes.previews == []


def test_no_sample_player_is_a_sentence(make: Build) -> None:
    with pytest.raises(RuntimeError, match="available"):
        make(preview_voice=None).preview("Charon")


def test_a_failed_sample_is_said_in_the_feed(svc: SetupService, fakes: Fakes, con: Any) -> None:
    fakes.preview_error = RuntimeError("no output device. pip install sounddevice")
    assert svc.preview("Charon")["ok"] is True  # it returned before playing
    rows = con.execute("SELECT payload FROM events WHERE kind='window.error'").fetchall()
    assert len(rows) == 1
    text = json.loads(rows[0][0])["text"]
    assert text == "I couldn't play a sample of Charon: No output device."
    # And the lock was released: the next sample plays.
    fakes.preview_error = None
    svc.preview("Charon")
    assert len(fakes.previews) == 1


def test_one_sample_at_a_time(make: Build, fakes: Fakes) -> None:
    pending: list[Callable[[], None]] = []
    svc = make(run_later=pending.append)
    assert svc.preview("Charon")["message"] == "Playing Charon."
    assert "still playing" in svc.preview("Charon")["message"]
    pending.pop()()
    assert svc.preview("Charon")["message"] == "Playing Charon."
    assert len(pending) == 1


def test_the_default_runner_is_a_daemon_thread(make: Build, fakes: Fakes) -> None:
    done = threading.Event()

    def play(voice: str, sentence: str) -> None:
        assert threading.current_thread() is not threading.main_thread()
        assert threading.current_thread().daemon
        done.set()

    svc = make(preview_voice=play, run_later=None)
    svc.preview("Charon")
    assert done.wait(3)


# ───────────────────────────── Claude Code ─────────────────────────────


def test_claude_sign_in(svc: SetupService, fakes: Fakes) -> None:
    reply = svc.sign_in_claude()
    assert reply == {"ok": True, "message": "The Claude Code sign-in is open in your browser."}
    assert fakes.logins == 1


def test_claude_sign_in_unavailable_or_failing_is_a_sentence(make: Build, fakes: Fakes) -> None:
    with pytest.raises(RuntimeError, match="isn't available"):
        make(claude_login=None).sign_in_claude()
    fakes.login_error = FileNotFoundError("claude: not found. Run pip install claude-agent-sdk")
    with pytest.raises(RuntimeError) as exc:
        make().sign_in_claude()
    assert "pip install" not in str(exc.value)


# ───────────────────────────── problems ─────────────────────────────


TERMINAL_REFUSAL = (
    "gemini_api_key is not set.\n"
    "  what it is for : the voice\n"
    "  store it       : python -m jarvis secrets set gemini_api_key"
)


def _refuse(con: Any, sentence: str, action: str | None, ts: str | None = None) -> None:
    publish(con, "desk.refused", "desk", {"sentence": sentence, "action": action})
    if ts is not None:
        con.execute("UPDATE events SET ts=? WHERE kind='desk.refused'", (ts,))
        con.commit()


def test_a_desk_refusal_is_a_problem_with_its_action_and_no_command(
    svc: SetupService, con: Any
) -> None:
    _refuse(con, TERMINAL_REFUSAL, "secret:gemini_api_key")
    s = svc.status()
    assert s["problems"] == [
        {
            "process": "desk",
            "sentence": "I need a Gemini API key before I can listen or speak.",
            "action": "secret:gemini_api_key",
        }
    ]
    _no_command(s)


@pytest.mark.parametrize(
    ("sentence", "action", "shown"),
    [
        (
            "wake model files missing: run python -m jarvis wake download",
            "wake",
            "The wake-word model isn't on this computer yet.",
        ),
        (
            "no device matching 'Jabra'. Present: 'Realtek', 'USB'",
            "device",
            "No device matching 'Jabra'. Present: 'Realtek', 'USB'.",
        ),
        (
            "no reader voice that can read a question word for word. The simplest is your "
            "system's own: `sudo apt install espeak-ng` on Linux.",
            "voice",
            "No reader voice that can read a question word for word.",
        ),
        ("python -m jarvis desk", None, "The desk couldn't start."),
        ("github_token is not set.", "secret:github_token", "The GitHub token is missing."),
    ],
)
def test_each_refusal_reads_as_a_sentence(
    con: Any, sentence: str, action: str | None, shown: str
) -> None:
    _refuse(con, sentence, action)
    (p,) = find_problems(con)
    assert p == {"process": "desk", "sentence": shown, "action": action}


def test_a_refusal_ends_when_the_desk_beats_again(con: Any) -> None:
    _refuse(con, TERMINAL_REFUSAL, "secret:gemini_api_key", ts="2026-10-05T10:00:00.000Z")
    assert len(find_problems(con)) == 1
    liveness.beat(con, "desk", state="asleep", now_ts="2026-10-05T10:05:00.000Z")
    assert find_problems(con) == []


def test_a_refusal_after_the_last_beat_is_current(con: Any) -> None:
    liveness.beat(con, "desk", state="asleep", now_ts="2026-10-05T09:00:00.000Z")
    liveness.gone(con, "desk", now_ts="2026-10-05T09:30:00.000Z")
    _refuse(con, "no device matching 'x'", "device", ts="2026-10-05T10:00:00.000Z")
    assert [p["action"] for p in find_problems(con)] == ["device"]


def test_the_supervisor_knows_best(con: Any) -> None:
    # The supervisor writes the goodbye beat AFTER a refusal, so without its
    # word the heartbeat rule would hide a desk that is still refusing.
    _refuse(con, TERMINAL_REFUSAL, "secret:gemini_api_key", ts="2026-10-05T10:00:00.000Z")
    liveness.gone(con, "desk", now_ts="2026-10-05T10:00:01.000Z")
    assert find_problems(con) == []
    held = {"desk": {"running": False, "held": True, "reason": "gemini_api_key is not set."}}
    assert [p["action"] for p in find_problems(con, held)] == ["secret:gemini_api_key"]
    starting = {"desk": {"running": True, "held": False, "reason": ""}}
    assert find_problems(con, starting) == []


def test_a_held_process_without_a_refusal_offers_a_restart(con: Any) -> None:
    procs = {
        "desk": {"running": True, "held": False, "reason": ""},
        "telegram": {
            "running": False,
            "held": True,
            "reason": "telegram_bot_token is not set.\n  store it : python -m jarvis secrets set x",
        },
        "schedule": {"running": False, "held": True, "reason": ""},
    }
    problems = find_problems(con, procs)
    assert problems == [
        {
            "process": "telegram",
            "sentence": "telegram_bot_token is not set.",
            "action": "restart:telegram",
        },
        {
            "process": "schedule",
            "sentence": "The scheduler stopped and is waiting.",
            "action": "restart:schedule",
        },
    ]


def test_a_held_desk_is_one_problem_not_two(con: Any) -> None:
    _refuse(con, TERMINAL_REFUSAL, "secret:gemini_api_key")
    procs = {"desk": {"running": False, "held": True, "reason": "gemini_api_key is not set."}}
    assert len(find_problems(con, procs)) == 1


def test_a_malformed_refusal_is_ignored(con: Any) -> None:
    publish(con, "desk.refused", "desk", {"sentence": 7, "action": ["x"]})
    (p,) = find_problems(con)
    assert p == {"process": "desk", "sentence": "The desk couldn't start.", "action": None}
    con.execute("UPDATE events SET payload='not json' WHERE kind='desk.refused'")
    con.commit()
    assert find_problems(con) == []


def test_status_asks_the_control_when_not_handed_processes(make: Build) -> None:
    class Control:
        def status(self) -> dict[str, Any]:
            return {"telegram": {"running": False, "held": True, "reason": "bad token"}}

    s = make(control=Control()).status()
    assert s["problems"] == [
        {"process": "telegram", "sentence": "Bad token.", "action": "restart:telegram"}
    ]
    # Handed processes win over the control.
    assert make(control=Control()).status(processes={})["problems"] == []


def test_an_unreadable_database_is_a_problem_not_a_crash(make: Build, tmp_path: Path) -> None:
    s = make(db_path=tmp_path / "no" / "such" / "dir" / "j.db").status()
    assert [p["process"] for p in s["problems"]] == ["database"]


def test_the_module_keeps_no_mutable_state() -> None:
    # Rule 3: the reader is another process. Module-level lists or dicts would
    # be state that one window thread could leak into another's answer.
    for name, value in vars(setup_mod).items():
        if name.startswith("__"):
            continue
        assert not isinstance(value, (list, dict, set)), name


# ───────────────────────────── pairing a phone ─────────────────────────────


def test_pairing_needs_the_bot_first(svc: SetupService) -> None:
    with pytest.raises(RuntimeError, match="bot token first"):
        svc.pair_phone()


def test_pairing_starts_a_bot_that_is_not_running_to_hear_the_code(
    make: Build, fakes: Fakes, keys: FakeSecrets
) -> None:
    # The bot redeems the code from the database, but only while it polls.
    class Control:
        def status(self) -> dict[str, Any]:
            return {"telegram": {"running": False, "held": False}}

    keys.stored["telegram_bot_token"] = "123456:" + "B" * 35
    reply = make(control=Control()).pair_phone()
    assert reply["code"] and reply["minutes"] == 10
    assert fakes.restarts == ["telegram"]
    _no_command(reply)


def test_a_running_bot_is_left_alone_when_pairing(
    make: Build, fakes: Fakes, keys: FakeSecrets
) -> None:
    class Control:
        def status(self) -> dict[str, Any]:
            return {"telegram": {"running": True, "held": False}}

    keys.stored["telegram_bot_token"] = "123456:" + "B" * 35
    make(control=Control()).pair_phone()
    assert fakes.restarts == []


# ───────────────────────────── a headset plugged in later ─────────────────────────────


def test_looking_again_rescans_the_hardware_before_listing(make: Build, fakes: Fakes) -> None:
    order: list[str] = []
    fakes_list = fakes.list_devices

    def listed() -> list[dict[str, Any]]:
        order.append("list")
        return fakes_list()

    svc = make(list_devices=listed, rescan_devices=lambda: order.append("rescan"))
    svc.status()
    assert order == ["list"], "an ordinary read costs PortAudio no restart"
    order.clear()
    svc.status(rescan=True)
    assert order == ["rescan", "list"]


def test_no_rescan_while_a_voice_sample_is_playing(make: Build) -> None:
    rescans: list[int] = []
    svc = make(rescan_devices=lambda: rescans.append(1))
    assert svc._preview_lock.acquire(blocking=False)
    try:
        svc.status(rescan=True)
    finally:
        svc._preview_lock.release()
    assert rescans == []


def test_the_real_rescan_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from jarvis.app import adapters

    broken = types.SimpleNamespace(_terminate=lambda: None, _initialize=lambda: 1 / 0)
    monkeypatch.setitem(sys.modules, "sounddevice", broken)
    adapters.rescan_devices()
