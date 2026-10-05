"""The callers of the computer abilities: each test fails if one goes missing.

Opening apps, running commands, saying whether Claude Code is signed in,
looking at the screen and ignoring a breath are all built and tested in their
own files. Those tests build their own registries and drive their own
TurnControllers, so every one of them stays green while the desk, the chats,
doctor and the window never call any of it. This file is where that would show.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from jarvis import __main__ as cli
from jarvis.config import Config

ROOT = Path(__file__).resolve().parent.parent


def _fn(name: str, path: str = "jarvis/__main__.py") -> str:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(node)


def _calls(name: str) -> set[str]:
    tree = ast.parse((ROOT / "jarvis/__main__.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    }


# ───────────────────────────── the voice detector ─────────────────────────────


def test_the_desk_turn_uses_the_chosen_vad_and_its_onset() -> None:
    """A K-of-W rule nobody passes is the 3-in-a-row rule every breath got through."""
    tree = ast.parse((ROOT / "jarvis/__main__.py").read_text(encoding="utf-8"))
    build = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_desk"
    )
    assert "_desk_vad" in _calls("_build_desk")
    assert "EnergyVad" not in _calls("_build_desk"), "loudness alone is the bug"
    assert "choose" in _calls("_desk_vad")
    turn_call = next(
        n
        for n in ast.walk(build)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TurnController"
    )
    assert {"vad", "idle_onset", "idle_preroll_ms", "preroll_ms", "max_turn_s"} <= {
        k.arg for k in turn_call.keywords
    }
    assert "_check_vad" in _calls("cmd_doctor")
    assert "cfg.voice.vad" in _fn("_desk_vad"), "the escape hatch must reach the choice"


def test_the_desk_without_a_vad_model_warns_and_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("numpy")
    from jarvis.audio import vadmodel

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(vadmodel, "search_dirs", lambda *a, **k: (tmp_path / "none",))
    monkeypatch.setattr(
        vadmodel, "_https_get", lambda url: (_ for _ in ()).throw(OSError("offline"))
    )
    choice = cli._desk_vad(Config())
    assert choice.name == "voiced" and choice.idle_onset == (3, 4)
    out = capsys.readouterr().out
    assert "voice activity" in out and "offline" in out


def test_an_unknown_vad_mode_is_a_refusal_not_a_guess() -> None:
    from jarvis.config import Voice

    with pytest.raises(cli.StartupRefused):
        cli._desk_vad(Config(voice=Voice(vad="loudest")))


def test_the_transcript_a_yes_is_read_from_leaves_hesitations_out() -> None:
    assert "without_fillers" in _fn("words", "jarvis/voice/tools.py")
    assert "without_fillers" in _fn("since", "jarvis/voice/tools.py")


# ───────────────────────────── this computer ─────────────────────────────


def test_every_channel_gets_the_desktop_the_probe_and_the_eyes() -> None:
    src = _fn("tool_extra")
    assert "pc.choose_desktop()" in src
    assert "cc_status.probe(claude_cli_path())" in src
    assert "GeminiVision(" in src and "choose_capturer()" in src


def test_the_desk_reads_the_app_list_before_the_first_request() -> None:
    assert ".warm()" in _fn("_build_desk")


def test_the_new_tools_are_built_in_and_offered_only_where_they_belong() -> None:
    from jarvis import effects as fx
    from jarvis.live.profiles import DESK, PHONE_USER
    from jarvis.tools.builtin import computer, pc, screen
    from jarvis.tools.default import BUILTIN, registry

    names = {t.name for t in (*pc.TOOLS, *computer.TOOLS, *screen.TOOLS)}
    assert names <= {t.name for t in BUILTIN}
    assert names <= set(DESK.tools)
    assert not names & set(PHONE_USER.tools), "a caller who only knows the number"
    reg = registry()
    assert names <= set(reg.names("desk")) and names <= set(reg.names("cli"))
    for remote in ("phone", "scheduler"):
        assert not names & set(reg.names(remote)), remote
    # Telegram may ASK whether Claude Code is signed in; it may never act here.
    assert set(reg.names("telegram")) & names == {"claude_code_status"}
    assert "pc.power_abort" in fx.registered_ops(), "a countdown nobody can cancel"


def test_a_machine_jarvis_cannot_drive_is_not_offered_the_pc_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_APP", raising=False)
    assert "open_app" in cli.withheld_tools(in_app=False, pc_available=False)
    assert "close_app" in cli.withheld_tools(in_app=False, pc_available=False)
    assert cli.withheld_tools(in_app=False, pc_available=True) == ()


def test_the_model_is_told_exactly_what_its_registry_can_do() -> None:
    src = _fn("heard_profile")
    for flag in ("pc=pc_on", "commands=commands", "vision=vision", "builds=builds"):
        assert flag in src, flag
    for name in ("cmd_chat", "_window_chat"):
        src = _fn(name)
        # ast.unparse writes single quotes.
        for flag in (
            "pc='open_app' in reg.names('cli')",
            "commands='run_command' in reg.names('cli')",
            "vision='look_at_screen' in reg.names('cli')",
        ):
            assert flag in src, (name, flag)
    assert "registry(without=withheld_tools())" in _fn("cmd_chat")


def test_the_persona_rules_follow_the_flags() -> None:
    from jarvis.live.persona import desk_instruction, phone_instruction, text_instruction

    on = desk_instruction(pc=True, commands=True, vision=True)
    off = desk_instruction(pc=False, commands=False, vision=False)
    assert on != off
    assert "claude_code_status" in on and "look_at_screen" in on and "terminal commands" in on
    assert "claude_code_status" not in off and "look_at_screen" not in off
    assert text_instruction(pc=True) != text_instruction(pc=False)
    phone = phone_instruction()
    assert "claude_code_status" not in phone and "look_at_screen" not in phone


def test_every_conversation_can_hear_a_yes() -> None:
    assert "confirmations=Confirmations()" in _fn("_build_desk")
    for name in ("cmd_chat", "_window_chat"):
        assert "TypedTurns()" in _fn(name), name


# ───────────────────────────── doctor and the self-test ─────────────────────────────


def test_doctor_says_what_this_computer_can_do() -> None:
    calls = _calls("cmd_doctor")
    assert {"_check_vad", "_check_computer", "_check_claude_cli"} <= calls
    assert "cc_status.probe(exe)" in _fn("_check_claude_cli")


def test_doctor_runs_the_computer_section_without_raising(
    capsys: pytest.CaptureFixture[str],
) -> None:
    r = cli.Report(lines=[])
    cli._check_computer(r)
    text = "\n".join(r.lines)
    assert "this computer" in text and "commands run in" in text and "screen:" in text
    assert not r.blocking, "nothing here may stop anything else from starting"


def test_the_built_app_checks_what_it_cannot_import_lazily() -> None:
    from jarvis.app import selftest

    names = {c.name for c in selftest.checks("win32")}
    assert {
        "voice activity",
        "terminal commands",
        "desktop control",
        "screen capture",
        "claude code sign-in",
    } <= names
    for module in (
        "jarvis.pc.windows",
        "jarvis.tools.builtin.pc",
        "jarvis.shell.run",
        "jarvis.cc.status",
        "jarvis.capture.look",
        "jarvis.capture.windows",
        "jarvis.tools.builtin.computer",
        "jarvis.tools.builtin.screen",
        "jarvis.audio.vadmodel",
        "jarvis.audio.fillers",
    ):
        assert module in selftest._MODULES, module


# ───────────────────────────── the window ─────────────────────────────


@pytest.fixture
def con(tmp_path: Path) -> sqlite3.Connection:
    from jarvis.db import connect, migrate

    c = connect(str(tmp_path / "j.db"))
    migrate(c)
    return c


def _feed(con: sqlite3.Connection) -> list[tuple[str, str]]:
    from jarvis.window import snapshot

    return [(i["role"], i["text"]) for i in snapshot.feed(con, after=0, limit=50)["items"]]


def test_the_feed_shows_the_read_back_the_command_and_what_it_printed(
    con: sqlite3.Connection,
) -> None:
    from jarvis.bus import publish

    publish(
        con,
        "confirm.proposed",
        "desk",
        {"tool": "run_command", "readback": "I'll run this. The command, exactly:\nGet-Date"},
    )
    publish(con, "confirm.granted", "desk", {"tool": "run_command", "by": "said"})
    publish(con, "shell.started", "desk", {"command": "Get-Date"})
    publish(
        con,
        "shell.finished",
        "desk",
        {"said": "The command finished.", "output": "Monday 5 October"},
    )
    publish(con, "confirm.refused", "desk", {"tool": "close_app", "why": "said no"})
    shown = _feed(con)
    assert (
        "system",
        "asked before run_command: I'll run this. The command, exactly:\nGet-Date",
    ) in (shown)
    assert ("system", "you said yes: run_command") in shown
    assert ("tool", "running: Get-Date") in shown
    assert ("tool", "The command finished.\nMonday 5 October") in shown
    assert ("system", "not done: close_app (said no)") in shown


def test_the_feed_always_shows_a_look_at_the_screen_and_a_countdown(
    con: sqlite3.Connection,
) -> None:
    from jarvis.bus import publish

    publish(con, "effect.recorded", "desk", {"kind": "capture.vision", "summary": "I looked"})
    publish(con, "effect.recorded", "desk", {"kind": "pc.power", "summary": "shutdown in 60s"})
    publish(con, "effect.recorded", "desk", {"kind": "pc.volume", "summary": "louder"})
    publish(con, "pc.power", "desk", {"action": "shutdown", "outcome": "called_off"})
    publish(con, "pc.sleep", "desk", {"outcome": "started", "error": None})
    shown = _feed(con)
    assert ("system", "I looked") in shown and ("system", "shutdown in 60s") in shown
    assert ("system", "louder") not in shown, "only what the user must always see"
    assert ("system", "shutdown called off") in shown
    assert ("system", "going to sleep") in shown


def test_the_tools_tab_gets_a_commands_output_not_just_its_summary() -> None:
    src = _fn("_api_tool", "jarvis/window/server.py")
    assert "'detail'" in src and "getattr(said, 'detail'" in src
    js = (ROOT / "jarvis/window/static/app.js").read_text(encoding="utf-8")
    assert "data.detail" in js and "tool-result-detail" in js
    html = (ROOT / "jarvis/window/static/index.html").read_text(encoding="utf-8")
    assert 'id="tool-result-detail"' in html


def test_the_window_never_cleans_a_command_out_of_its_own_read_back() -> None:
    # The in-app cleaner takes terminal instructions out of refusals; applied to
    # a read-back it would hide the very command being asked about.
    js = (ROOT / "jarvis/window/static/app.js").read_text(encoding="utf-8")
    for kind in ("confirm.proposed", "shell.started", "shell.finished"):
        assert f'"{kind}"' in js, kind
    assert "VERBATIM_KINDS.has(" in js
