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
    assert "blocking problem" in out


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


def test_the_desk_listens_for_event_kinds_that_actually_exist() -> None:
    """A kind the session never emits is a branch that never fires, silently.

    Both sides of this seam are strings, nothing validates them, and the first
    draft listened for "reconnect" and "tool_error" — neither of which
    ``LiveSession`` has ever emitted. The terminal simply stayed quiet, which is
    indistinguishable from "nothing happened".
    """
    import ast
    from pathlib import Path

    import jarvis.live.session as live

    tree = ast.parse(Path(live.__file__).read_text(encoding="utf-8"))
    emitted = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_emit"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert "input_transcript" in emitted, "the audit found the emitter, not this test's parser"
    unknown = sorted(set(cli.DESK_EVENTS) - emitted)
    assert not unknown, f"the desk listens for kinds nothing emits: {unknown}"


# ───────────────────────── doctor tells the truth about desk ─────────────────


class FakeProbe:
    def __init__(self, devices: list, defaults: tuple) -> None:
        self._devices, self._defaults = devices, defaults

    def devices(self) -> list:
        return self._devices

    def defaults(self) -> tuple:
        return self._defaults


def _devices():
    from jarvis.audio.devices import DeviceInfo

    return {
        "headset": DeviceInfo(0, "Jabra Evolve2 40", "ALSA", 1, 2, 48000.0),
        "mic": DeviceInfo(1, "MacBook Pro Microphone", "CoreAudio", 1, 0, 48000.0),
        "speakers": DeviceInfo(2, "MacBook Pro Speakers", "CoreAudio", 0, 2, 48000.0),
    }


def use_probe(monkeypatch: pytest.MonkeyPatch, devices: list, defaults: tuple) -> None:
    import jarvis.audio.devices as dev

    monkeypatch.setattr(dev, "PortAudioProbe", lambda: FakeProbe(devices, defaults))
    monkeypatch.setattr(cli, "_installed", lambda module: True)


def test_doctor_does_not_pass_a_machine_where_desk_cannot_start(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The worst available bug in a command whose whole job is to say "you are ready".

    A laptop whose default input is the built-in mic and whose default output is
    the built-in speakers is TWO devices and two clocks; ``select_duplex_device``
    raises ClockSplit and ``desk`` exits 2. An earlier version of this check only
    looked at whether any device was full-duplex, so it printed "Everything
    required is present" on exactly that machine.
    """
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "not-a-real-key")
    d = _devices()
    use_probe(monkeypatch, [d["mic"], d["speakers"]], (1, 2))

    assert run(["doctor"], workspace / "j.db") == 1
    out = capsys.readouterr().out
    assert "ClockSplit" in out
    assert "python -m jarvis desk" in out


def test_doctor_passes_the_machine_where_desk_can_start(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "not-a-real-key")
    d = _devices()
    use_probe(monkeypatch, [d["headset"]], (0, 0))

    assert run(["doctor"], workspace / "j.db") == 0
    out = capsys.readouterr().out
    assert "desk would use: Jabra Evolve2 40" in out
    assert "Nothing blocking" in out


def test_doctor_runs_the_same_device_selection_the_desk_runs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a parallel list of conditions — the same function, so they cannot drift."""
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "k")
    d = _devices()
    use_probe(monkeypatch, [d["headset"], d["mic"]], (0, 0))

    cfgfile = workspace / "c.toml"
    cfgfile.write_text('[voice]\ninput_device = "Nonexistent Headset"\n', encoding="utf-8")
    code = cli.main(["--db", str(workspace / "j.db"), "--config", str(cfgfile), "doctor"])
    out = capsys.readouterr().out
    # desk would refuse with DeviceVanished; doctor must refuse with the same words.
    assert code == 1
    assert "DeviceVanished" in out
    assert "Nonexistent Headset" in out


def test_the_readiness_list_names_every_process_and_its_blocker(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A front door that does not mention the other three doors is not a front door."""
    d = _devices()
    use_probe(monkeypatch, [d["headset"]], (0, 0))
    run(["doctor"], workspace / "j.db")
    out = capsys.readouterr().out
    section = out.split("what you can run")[1]
    for command in (
        "python -m jarvis status",
        "python -m jarvis desk",
        "python -m jarvis.cc",
        "python -m jarvis.telegram",
        "python -m jarvis.schedule",
    ):
        assert command in section, command
    assert "no gemini_api_key" in section
    # And it must not pretend the driver is usable without a way to make a job.
    assert "no command creates the job row" in section


def test_doctor_checks_the_cli_the_driver_would_actually_run() -> None:
    """The SDK bundles its own CLI and prefers it; PATH is only its fallback.

    Checking PATH alone is wrong in both directions — "missing" where
    `pip install -e '.[cc]'` is sufficient, and the version of a binary the
    driver will never execute, on a repo whose measured facts are pinned to one
    CLI build.
    """
    found = cli.claude_cli_path()
    if found is None:  # pragma: no cover - the extra is installed in CI
        pytest.skip("no claude CLI here at all")
    import shutil as _shutil

    if _installed_sdk():
        assert "claude_agent_sdk" in found, f"doctor would report {_shutil.which('claude')} instead"


def _installed_sdk() -> bool:
    import importlib.util

    return importlib.util.find_spec("claude_agent_sdk") is not None
