"""A missing config file is a valid configuration; a credential in one is not.

The refusal is the test worth having. Everything else here is defaults, and
defaults that work are the difference between "first run" and "scavenger hunt".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import config as cfgmod


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_no_file_is_a_valid_configuration(tmp_path: Path) -> None:
    cfg = cfgmod.load(tmp_path / "nope.toml")
    assert cfg.voice.model == "gemini-3.8-live"
    assert cfg.tz == "Europe/Istanbul"
    assert cfg.desk.permission_mode == "plan"


def test_the_shipped_example_parses_and_round_trips(tmp_path: Path) -> None:
    """`config init` writes EXAMPLE; a file that does not load is a broken command."""
    cfg = cfgmod.load(write(tmp_path / "c.toml", cfgmod.EXAMPLE))
    assert cfg.desk.github_owner == "Efkrdnz"
    assert cfg.briefing.sections == ("projects", "inbox", "issues", "comments")
    assert cfg.voice.assume_headset is True


def test_sections_become_a_tuple_not_a_list(tmp_path: Path) -> None:
    """The dataclass is frozen; a list inside it is a verdict any caller can edit."""
    cfg = cfgmod.load(write(tmp_path / "c.toml", '[briefing]\nsections = ["inbox"]\n'))
    assert cfg.briefing.sections == ("inbox",)


@pytest.mark.parametrize(
    "body",
    [
        'api_key = "sk-live-1"',
        '[telegram]\ntoken = "123:abc"',
        '[voice]\nsecret = "x"',
        '[phone]\npin = "4821"',
        '[github]\npassword = "hunter2"',
    ],
)
def test_a_credential_in_the_config_is_refused_not_warned_about(tmp_path: Path, body: str) -> None:
    with pytest.raises(cfgmod.SecretInConfig) as exc:
        cfgmod.load(write(tmp_path / "c.toml", body + "\n"))
    assert "keyring" in str(exc.value) or "secrets set" in str(exc.value)


def test_the_exemptions_are_not_mistaken_for_credentials(tmp_path: Path) -> None:
    cfgmod.load(write(tmp_path / "c.toml", '[voice]\nkeyring_service = "jarvis"\n'))


def test_an_unknown_key_does_not_stop_an_older_jarvis_starting(tmp_path: Path) -> None:
    cfg = cfgmod.load(write(tmp_path / "c.toml", "[voice]\nfrom_the_future = true\n"))
    assert cfg.voice.model == "gemini-3.8-live"


def test_a_malformed_file_is_loud_rather_than_silently_default(tmp_path: Path) -> None:
    import tomllib

    with pytest.raises(tomllib.TOMLDecodeError):
        cfgmod.load(write(tmp_path / "c.toml", "[voice\nmodel = 'x'\n"))


def test_the_env_var_wins_over_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "mine.toml"))
    assert cfgmod.default_path() == tmp_path / "mine.toml"
    monkeypatch.delenv("JARVIS_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cfgmod.default_path() == tmp_path / "jarvis" / "config.toml"


def test_the_workspace_is_expanded_only_when_it_is_used(tmp_path: Path) -> None:
    cfg = cfgmod.load(write(tmp_path / "c.toml", '[desk]\nworkspace = "~/code"\n'))
    assert cfg.desk.workspace == "~/code"  # the value in the file is the value in the row
    assert cfg.workspace_path.is_absolute()
