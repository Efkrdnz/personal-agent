"""The app's settings overlay: app-settings.toml, laid over a config.toml it never touches.

The window is a form, and a form can send anything: a "false" that arrives as a
string, a list where a word belongs, an API key pasted into the city box. Every
refusal here is one of those, and every one must leave the files as they were.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from jarvis import config as cfgmod

HAND_WRITTEN = """\
# my own notes about why the wake threshold is low — keep this comment
tz = "Europe/Istanbul"

[voice]
wake_threshold = 0.35   # the fan is loud
vocabulary = ["quote", "Kadıköy"]
model = "gemini-3.8-live"

[location]
city = "Ankara"
units = "metric"
"""


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis" / "config.toml"
    p.parent.mkdir()
    p.write_text(HAND_WRITTEN, encoding="utf-8")
    return p


def _overlay(cfg_path: Path) -> dict:
    return tomllib.loads(cfgmod.settings_path(cfg_path).read_text(encoding="utf-8"))


# ───────────────────────────── the shape ─────────────────────────────


def test_new_sections_and_defaults() -> None:
    cfg = cfgmod.Config()
    assert cfg.persona == cfgmod.Persona(address="sir", name="")
    assert cfg.app == cfgmod.App(
        start_with_windows=False, start_telegram=True, open_window_on_start=True
    )
    assert cfg.voice.gemini_voice == "Charon"
    assert cfg.voice.reader_voice == "en-GB-RyanNeural"


def test_the_example_carries_the_new_sections(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(cfgmod.EXAMPLE, encoding="utf-8")
    cfg = cfgmod.load(p)
    assert cfg.persona.address == "sir"
    assert cfg.app.start_telegram is True
    assert cfg.voice.gemini_voice == "Charon"
    assert cfg.voice.reader_voice == "en-GB-RyanNeural"


def test_settings_path_is_a_sibling_of_config_toml(tmp_path: Path, monkeypatch) -> None:
    assert cfgmod.settings_path(tmp_path / "x" / "config.toml") == (
        tmp_path / "x" / "app-settings.toml"
    )
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "mine.toml"))
    assert cfgmod.settings_path() == tmp_path / "app-settings.toml"


def test_every_settable_key_is_a_real_field_of_a_type_the_writer_handles() -> None:
    # A typo in SETTABLE would be a setting the window offers and no save can
    # ever reach. _annotation raises KeyError on one.
    handled = {"bool", "int", "float", "str", "str | None", "tuple[str, ...]"}
    for key in cfgmod.SETTABLE:
        section, _, name = key.rpartition(".")
        assert cfgmod._annotation(section, name) in handled, key


# ───────────────────────────── save and merge ─────────────────────────────


def test_save_writes_the_overlay_and_never_touches_config_toml(cfg_path: Path) -> None:
    before = cfg_path.read_bytes()
    cfg = cfgmod.save_setting("voice.gemini_voice", "Puck", cfg_path)
    assert cfg_path.read_bytes() == before, "config.toml (and its comments) must be untouched"
    assert cfg.voice.gemini_voice == "Puck"
    assert _overlay(cfg_path) == {"voice": {"gemini_voice": "Puck"}}


def test_the_overlay_merges_key_by_key_not_section_by_section(cfg_path: Path) -> None:
    cfg = cfgmod.save_setting("location.units", "imperial", cfg_path)
    # The other keys of [location] and [voice] still come from config.toml.
    assert cfg.location.units == "imperial"
    assert cfg.location.city == "Ankara"
    assert cfg.voice.wake_threshold == 0.35
    assert cfg.voice.vocabulary == ("quote", "Kadıköy")
    assert cfgmod.load(cfg_path) == cfg


def test_saves_accumulate_and_the_last_one_wins(cfg_path: Path) -> None:
    cfgmod.save_setting("persona.address", "boss", cfg_path)
    cfgmod.save_setting("persona.name", "Efe", cfg_path)
    cfgmod.save_setting("persona.address", "ma'am", cfg_path)
    cfgmod.save_setting("tz", "Europe/London", cfg_path)
    cfg = cfgmod.load(cfg_path)
    assert cfg.persona == cfgmod.Persona(address="ma'am", name="Efe")
    assert cfg.tz == "Europe/London"
    # Top-level keys come before any table, or TOML would file them under one.
    assert _overlay(cfg_path) == {
        "tz": "Europe/London",
        "persona": {"address": "ma'am", "name": "Efe"},
    }


def test_an_overlay_works_without_any_config_toml(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    cfg = cfgmod.save_setting("app.start_with_windows", True, p)
    assert not p.exists()
    assert cfg.app.start_with_windows is True
    assert cfgmod.load(p).app.start_with_windows is True


def test_the_settings_directory_is_created(tmp_path: Path) -> None:
    p = tmp_path / "not" / "yet" / "config.toml"
    cfgmod.save_setting("location.city", "İzmir", p)
    assert cfgmod.load(p).location.city == "İzmir"


@pytest.mark.parametrize(
    "text",
    [
        'O"Brien \\ the second',
        "Kadıköy — İstanbul",
        "emoji 🛰 and tabs nbsp",
        "'single' and #hash and = equals",
    ],
)
def test_strings_round_trip_through_the_emitter(tmp_path: Path, text: str) -> None:
    p = tmp_path / "config.toml"
    assert cfgmod.save_setting("persona.name", text, p).persona.name == text.strip()


def test_lists_become_tuples_trimmed_and_deduplicated(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    cfg = cfgmod.save_setting("voice.vocabulary", [" quote ", "Jarvis", "", "quote"], p)
    assert cfg.voice.vocabulary == ("quote", "Jarvis")
    assert cfgmod.save_setting("voice.languages", ("en-US", "tr-TR"), p).voice.languages == (
        "en-US",
        "tr-TR",
    )


def test_an_int_is_a_fine_float(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    cfg = cfgmod.save_setting("voice.wake_threshold", 0.42, p)
    assert cfg.voice.wake_threshold == 0.42
    assert _overlay(p)["voice"]["wake_threshold"] == 0.42


def test_system_default_microphone_is_saved_as_empty_and_read_as_none(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    assert cfgmod.save_setting("voice.input_device", "Jabra", p).voice.input_device == "Jabra"
    # None cannot be written to TOML; "" is how the file says "default", and
    # load() must not hand "" on, because a device search for "" matches all.
    assert cfgmod.save_setting("voice.input_device", None, p).voice.input_device is None
    assert _overlay(p)["voice"]["input_device"] == ""
    assert cfgmod.save_setting("voice.input_device", "", p).voice.input_device is None


def test_an_empty_input_device_in_config_toml_means_the_default(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[voice]\ninput_device = ""\noutput_device = ""\n', encoding="utf-8")
    cfg = cfgmod.load(p)
    assert cfg.voice.input_device is None and cfg.voice.output_device is None


def test_keys_the_app_does_not_own_survive_a_rewrite(cfg_path: Path) -> None:
    overlay = cfgmod.settings_path(cfg_path)
    overlay.write_text('[briefing]\nat_local = "09:30"\n', encoding="utf-8")
    cfg = cfgmod.save_setting("persona.name", "Efe", cfg_path)
    assert cfg.briefing.at_local == "09:30"
    assert _overlay(cfg_path)["briefing"] == {"at_local": "09:30"}


# ───────────────────────────── refusals ─────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "desk.permission_mode",  # "dontAsk" through a form would deny AskUserQuestion
        "voice.model",
        "telegram.token",
        "persona",
        "",
        "voice.gemini_voice.extra",
        "spend_threshold_usd",
    ],
)
def test_a_key_outside_settable_is_refused(cfg_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="not a setting"):
        cfgmod.save_setting(key, "x", cfg_path)
    assert not cfgmod.settings_path(cfg_path).exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("app.start_with_windows", "false"),  # a truthy string is a switch stuck on
        ("app.start_with_windows", 0),
        ("app.start_telegram", None),
        ("voice.wake_threshold", "0.5"),
        ("voice.wake_threshold", True),
        ("voice.wake_threshold", float("nan")),
        ("voice.wake_threshold", float("inf")),
        ("persona.name", 7),
        ("persona.name", ["Efe"]),
        ("voice.vocabulary", "quote, Jarvis"),
        ("voice.vocabulary", ["quote", 3]),
        ("location.city", {"name": "Ankara"}),
        ("persona.address", None),
    ],
)
def test_a_value_of_the_wrong_type_is_refused(cfg_path: Path, key: str, value: object) -> None:
    with pytest.raises(ValueError):
        cfgmod.save_setting(key, value, cfg_path)
    assert not cfgmod.settings_path(cfg_path).exists()


@pytest.mark.parametrize(
    ("key", "value", "words"),
    [
        ("location.units", "furlongs", "metric or imperial"),
        ("voice.wake_threshold", 1.5, "between 0 and 1"),
        ("voice.wake_threshold", 0.0, "between 0 and 1"),
        ("tz", "Mars/Olympus_Mons", "time zone"),
        ("tz", "../../etc/passwd", "time zone"),
        ("tz", "", "empty"),
        ("persona.address", "   ", "empty"),
        ("voice.wake_word", "Hey Jarvis!", "model name"),
        ("persona.name", "line\nbreak", "line breaks"),
        ("persona.name", "nul\x00here", "control"),
        ("persona.name", "x" * 201, "too long"),
        ("voice.vocabulary", ["x" * 101], "too long"),
        ("voice.vocabulary", ["w"] * 101, "at most"),
        ("persona.name", "\ud800", "cannot be saved"),
    ],
)
def test_values_that_type_check_but_make_no_sense_are_refused_with_a_sentence(
    cfg_path: Path, key: str, value: object, words: str
) -> None:
    with pytest.raises(ValueError, match=words):
        cfgmod.save_setting(key, value, cfg_path)
    assert not cfgmod.settings_path(cfg_path).exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("location.city", "AIzaSyD-0123456789abcdefghijklmnopqrstu"),
        ("persona.name", "123456789:AAH0123456789abcdefghijklmnopqrstuv"),
        ("voice.vocabulary", ["quote", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"]),
        ("voice.reader_voice", "sk-ant-api03-0123456789abcdefghij"),
        ("persona.name", "-----BEGIN RSA PRIVATE KEY-----"),
        ("location.city", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"),
    ],
)
def test_a_credential_typed_into_a_setting_is_refused(
    cfg_path: Path, key: str, value: object
) -> None:
    with pytest.raises(cfgmod.SecretInConfig) as exc:
        cfgmod.save_setting(key, value, cfg_path)
    assert not cfgmod.settings_path(cfg_path).exists()
    # The sentence is for the window: it names the fix without a terminal command.
    assert "keyring" in str(exc.value)
    assert "python -m" not in str(exc.value)
    # ...and it never repeats the credential back.
    for text in value if isinstance(value, list) else [value]:
        if len(text) > 20:
            assert text not in str(exc.value)


def test_ordinary_long_words_are_not_mistaken_for_credentials(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    cfg = cfgmod.save_setting(
        "voice.vocabulary",
        ["Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch", "en-GB-ThomasNeural"],
        p,
    )
    assert len(cfg.voice.vocabulary) == 2


def test_a_refused_value_leaves_an_existing_overlay_byte_for_byte(cfg_path: Path) -> None:
    cfgmod.save_setting("persona.name", "Efe", cfg_path)
    before = cfgmod.settings_path(cfg_path).read_bytes()
    with pytest.raises(ValueError):
        cfgmod.save_setting("location.units", "cubits", cfg_path)
    assert cfgmod.settings_path(cfg_path).read_bytes() == before


def test_a_credential_written_into_the_overlay_by_hand_is_refused_on_load(cfg_path: Path) -> None:
    cfgmod.settings_path(cfg_path).write_text('[telegram]\ntoken = "1:x"\n', encoding="utf-8")
    with pytest.raises(cfgmod.SecretInConfig):
        cfgmod.load(cfg_path)


def test_a_broken_overlay_is_loud_and_names_itself(cfg_path: Path) -> None:
    cfgmod.settings_path(cfg_path).write_text("[voice\n", encoding="utf-8")
    with pytest.raises(ValueError, match="app-settings.toml") as exc:
        cfgmod.load(cfg_path)
    assert "delete it" in str(exc.value)


# ───────────────────────────── atomicity ─────────────────────────────


def test_the_write_is_atomic_and_leaves_no_temp_file(cfg_path: Path) -> None:
    cfgmod.save_setting("persona.name", "Efe", cfg_path)
    assert sorted(p.name for p in cfg_path.parent.iterdir()) == ["app-settings.toml", "config.toml"]


def test_a_failed_rename_keeps_the_old_file_and_cleans_up(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfgmod.save_setting("persona.name", "Efe", cfg_path)
    before = cfgmod.settings_path(cfg_path).read_bytes()

    def broken(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cfgmod.os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        cfgmod.save_setting("persona.name", "Someone else", cfg_path)
    assert cfgmod.settings_path(cfg_path).read_bytes() == before
    assert sorted(p.name for p in cfg_path.parent.iterdir()) == ["app-settings.toml", "config.toml"]


def test_a_briefly_locked_file_is_retried(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows: another process reading the file this instant makes the rename fail.
    real = os.replace
    calls = {"n": 0}

    def flaky(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(cfgmod.os, "replace", flaky)
    monkeypatch.setattr(cfgmod.time, "sleep", lambda s: None)
    assert cfgmod.save_setting("persona.name", "Efe", cfg_path).persona.name == "Efe"
    assert calls["n"] == 3


# ───────────────────────────── the spine rule ─────────────────────────────


def test_saving_works_under_python_dash_s(tmp_path: Path) -> None:
    """config.py is spine: it must import AND save with no third-party package."""
    src = Path(cfgmod.__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from jarvis import config;"
        "c = config.save_setting('location.units', 'imperial', sys.argv[2]);"
        "c = config.save_setting('voice.vocabulary', ['quote'], sys.argv[2]);"
        "print(c.location.units, c.voice.vocabulary)"
    )
    r = subprocess.run(
        [sys.executable, "-S", "-c", code, str(src), str(tmp_path / "config.toml")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    # Not tz: on Windows the zone data is the tzdata PACKAGE, which -S hides.
    assert r.stdout.strip() == "imperial ('quote',)"
