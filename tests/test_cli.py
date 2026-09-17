"""``python -m jarvis`` — the commands somebody runs before anything works.

``doctor`` is the one that matters. A voice assistant fails at startup with no
screen and no log the user will find, so "why won't it start" has to be one
command that names the gap and the command that closes it — and it must NEVER
print a credential while doing so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import __main__ as cli
from jarvis import secrets


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine with nothing configured. Every secret unset, every path temporary."""
    for s in secrets.SECRETS:
        monkeypatch.delenv(s.env, raising=False)
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def run(argv: list[str], db: Path | None = None) -> int:
    return cli.main([*(["--db", str(db)] if db else []), *argv])


# ───────────────────────────── doctor ─────────────────────────────


def test_doctor_fails_while_the_required_credential_is_missing(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["doctor"], workspace / "j.db") == 1
    out = capsys.readouterr().out
    assert "gemini_api_key" in out
    assert "python -m jarvis secrets set gemini_api_key" in out
    assert "must be fixed" in out


def test_doctor_never_prints_a_credential(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "sk-this-must-never-appear")
    monkeypatch.setenv("JARVIS_GITHUB_TOKEN", "ghp-also-never")
    run(["doctor"], workspace / "j.db")
    out = capsys.readouterr().out
    assert "this-must-never-appear" not in out
    assert "also-never" not in out
    assert "gemini_api_key  (environment" in out


def test_doctor_reports_the_tool_surface_and_the_drift(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["doctor"], workspace / "j.db")
    out = capsys.readouterr().out
    assert "code_build" in out
    # Named in DESK's profile, not built yet. Invisible everywhere else.
    assert "do not exist yet" in out


def test_doctor_creates_and_checks_the_database(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dbpath = workspace / "made-by-doctor.db"
    run(["doctor"], dbpath)
    assert dbpath.exists()
    assert "activity log chain verifies" in capsys.readouterr().out


def test_doctor_says_which_config_file_it_read(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["doctor"], workspace / "j.db")
    out = capsys.readouterr().out
    assert "config.toml does not exist" in out
    assert "using defaults" in out


def test_doctor_refuses_a_config_with_a_credential_in_it(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = workspace / "bad.toml"
    bad.write_text('[telegram]\ntoken = "123:abc"\n', encoding="utf-8")
    assert cli.main(["--db", str(workspace / "j.db"), "--config", str(bad), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "looks like a credential" in out
    assert "123:abc" not in out


# ───────────────────────────── config ─────────────────────────────


def test_config_init_writes_a_file_that_loads(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = workspace / "c.toml"
    assert cli.main(["--config", str(target), "config", "init"]) == 0
    assert target.exists()
    from jarvis import config as cfgmod

    assert cfgmod.load(target).desk.model == "claude-opus-5"


def test_config_init_will_not_clobber_without_force(workspace: Path) -> None:
    target = workspace / "c.toml"
    target.write_text("tz = 'UTC'\n", encoding="utf-8")
    assert cli.main(["--config", str(target), "config", "init"]) == 1
    assert target.read_text(encoding="utf-8") == "tz = 'UTC'\n"
    assert cli.main(["--config", str(target), "config", "init", "--force"]) == 0
    assert "spend_threshold_usd" in target.read_text(encoding="utf-8")


def test_config_show_marks_defaults_as_defaults(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["--config", str(workspace / "absent.toml"), "config", "show"])
    assert "does not exist — these are the defaults" in capsys.readouterr().out


def test_config_path_prints_one_line(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip().endswith("config.toml")


# ───────────────────────────── secrets ─────────────────────────────


def test_secrets_list_exits_nonzero_while_a_required_one_is_missing(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["secrets", "list"]) == 1
    out = capsys.readouterr().out
    assert out.count("MISSING") == 1
    assert "not set" in out


def test_secrets_set_without_a_name_says_which_names_exist(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["secrets", "set"]) == 1
    assert "gemini_api_key" in capsys.readouterr().err


def test_a_secret_is_never_taken_from_argv() -> None:
    """It would be in the shell history and in `ps`. The parser has nowhere to put it."""
    parser = cli.build_parser()
    args = parser.parse_args(["secrets", "set", "gemini_api_key"])
    assert not hasattr(args, "value")
    with pytest.raises(SystemExit):
        parser.parse_args(["secrets", "set", "gemini_api_key", "sk-oops"])


# ───────────────────────────── tools and status ─────────────────────────────


def test_tools_shows_the_phone_cannot_build(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["tools"]) == 0
    out = capsys.readouterr().out
    desk = out.split("desk:")[1].split("telegram:")[0]
    phone = out.split("phone:")[1].split("scheduler:")[0]
    assert "code_build" in desk
    assert "code_build" not in phone


def test_tools_verbose_prints_what_the_model_reads(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["tools", "-v"])
    assert "Start a new coding project" in capsys.readouterr().out


def test_status_runs_against_an_empty_database(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["status"], workspace / "j.db") == 0
    out = capsys.readouterr().out
    assert "presence:" in out
    # Zero priced is not the same as free — R5's wording, not this file's.
    assert "Nothing recorded" in out or "ceiling" in out


# ───────────────────────────── desk ─────────────────────────────


def test_desk_refuses_with_instructions_rather_than_a_traceback(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["desk"], workspace / "j.db") == 2
    err = capsys.readouterr().err
    assert "gemini_api_key is not set" in err
    assert "python -m jarvis secrets set gemini_api_key" in err


def test_desk_names_the_missing_packages_when_there_are_any(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(cli, "_installed", lambda module: module != "numpy")
    assert run(["desk"], workspace / "j.db") == 2
    err = capsys.readouterr().err
    assert "numpy" in err
    assert "python -m jarvis doctor" in err


# ───────────────────────────── the parser ─────────────────────────────


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_every_subcommand_has_a_handler() -> None:
    parser = cli.build_parser()
    for command in ("doctor", "secrets", "config", "status", "tools", "desk"):
        argv = [command, "list"] if command == "secrets" else [command]
        assert callable(parser.parse_args(argv).fn), command
