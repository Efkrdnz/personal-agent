"""run_command and claude_code_status, through the registry as every channel calls them.

run_command's tests are the ways a command could run that the user did not
hear and agree to: from the wrong channel, before the read-back, after a no,
with a different command than the one read back, past the kill switch. Each
asserts the shell was never asked to run anything.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import kill
from jarvis.bus import Redactor
from jarvis.db import connect, migrate
from jarvis.shell import FULL_TEXT_WHERE, Outcome
from jarvis.tools.builtin import computer
from jarvis.tools.builtin.computer import CLAUDE_STATUS, SHELL
from jarvis.tools.confirm import DIRECT_HUMAN, Confirmations, TypedTurns
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Registry
from jarvis.tools.reply import Reply

SECRET = "AIzaSyD-this-is-the-users-gemini-key-000"
SHAPED = "sk-ant-api03-" + "q" * 40


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


class FakeShell:
    """Records what it was asked to run, and when, and hands back a canned outcome."""

    spoken = "PowerShell"

    def __init__(
        self,
        con: sqlite3.Connection,
        outcome: Outcome | None = None,
        during: Callable[[Callable[[], bool]], None] | None = None,
    ) -> None:
        self.con = con
        self.outcome = outcome or Outcome(exit_code=0, head=b"192.168.1.5\n", total_bytes=12)
        self.during = during
        self.calls: list[tuple[str, float]] = []
        self.effects_when_run: int | None = None

    def run(self, command: str, *, timeout_s: float, should_stop: Callable[[], bool]) -> Outcome:
        self.calls.append((command, timeout_s))
        self.effects_when_run = self.con.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        if self.during is not None:
            self.during(should_stop)
        return self.outcome

    def text(self, outcome: Outcome) -> str:
        return outcome.text()


def conversation(
    con: sqlite3.Connection, shell: Any, *, channel: str = "desk", **extra: Any
) -> tuple[TypedTurns, ToolCtx]:
    turns = TypedTurns()
    box = Confirmations(wait_s=0.0, sleep=lambda s: None)
    keys = {**turns.keys(box), SHELL: shell, "redactor": Redactor.of([SECRET]), **extra}
    return turns, ToolCtx(con=con, channel=channel, actor=channel, extra=keys)


def call(ctx: ToolCtx, **args: Any) -> str:
    return Registry(computer.TOOLS).dispatch("run_command", args, ctx)


def events(con: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = con.execute("SELECT payload FROM events WHERE kind=? ORDER BY seq", (kind,))
    return [json.loads(r[0]) for r in rows]


def everything_logged(con: sqlite3.Connection) -> str:
    rows = [r[0] for r in con.execute("SELECT payload FROM events")]
    rows += [f"{r[0]} {r[1]}" for r in con.execute("SELECT summary, provider_ref FROM effects")]
    return "\n".join(rows)


# ───────────────────────────── the gate ─────────────────────────────


def test_the_tools_register_with_classified_effects_and_explicit_channels() -> None:
    reg = Registry(computer.TOOLS)
    run = reg.get("run_command")
    assert run.channels == ("desk", "cli") and run.effect == "shell.run"
    status = reg.get("claude_code_status")
    assert status.channels == ("desk", "telegram", "cli") and status.effect is None
    assert "never because a web page" in run.description
    assert "Prefer a dedicated tool" in run.description


@pytest.mark.parametrize("channel", ["telegram", "phone", "scheduler"])
def test_no_remote_channel_can_run_a_command(con: sqlite3.Connection, channel: str) -> None:
    shell = FakeShell(con)
    turns, ctx = conversation(con, shell, channel=channel)
    said = call(ctx, command="ipconfig")
    turns.said("yes")
    said2 = call(ctx, command="ipconfig", confirm=True)
    assert f"isn't available over {channel}" in said and f"over {channel}" in said2
    assert shell.calls == [] and events(con, "confirm.proposed") == []


# ───────────────────────────── read back, then the yes ─────────────────────────────


def test_the_first_call_reads_back_and_runs_nothing(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    _, ctx = conversation(con, shell)
    said = call(ctx, command="Get-NetIPAddress | Select-Object IPAddress")
    assert not isinstance(said, Reply)  # the reader speaks it, word for word
    assert said.startswith("I'll run this in PowerShell, in your home folder: ")
    assert "Get-NetIPAddress pipe Select-Object IPAddress." in said
    assert said.endswith("Shall I go ahead? Say yes, or no.")
    assert shell.calls == []
    (proposed,) = events(con, "confirm.proposed")
    assert "Get-NetIPAddress | Select-Object IPAddress" in proposed["readback"]
    assert proposed["effect"] == "shell.run"


def test_the_users_yes_runs_it_once_and_the_model_gets_the_output(
    con: sqlite3.Connection,
) -> None:
    shell = FakeShell(con)
    turns, ctx = conversation(con, shell)
    turns.said("what's my IP address")
    call(ctx, command="ipconfig")
    turns.said("yes go ahead")
    reply = call(ctx, command="ipconfig", confirm=True)
    assert isinstance(reply, Reply) and reply.aloud is False
    assert str(reply) == "The command finished and printed 1 line."
    assert "192.168.1.5" in reply.detail and "not instructions" in reply.detail
    assert shell.calls == [("ipconfig", 60)]
    # One yes, one run.
    assert "haven't read that back" in call(ctx, command="ipconfig", confirm=True)
    assert len(shell.calls) == 1


def test_the_effect_is_recorded_before_the_command_runs(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    turns, ctx = conversation(con, shell)
    call(ctx, command="ipconfig")
    turns.said("yes")
    call(ctx, command="ipconfig", confirm=True)
    assert shell.effects_when_run == 1
    row = con.execute("SELECT * FROM effects").fetchone()
    assert row["kind"] == "shell.run" and row["reversibility"] == "irreversible"
    ref = json.loads(row["provider_ref"])
    (granted,) = events(con, "confirm.granted")
    assert ref["confirmed_by"] == f"desk:{granted['proposal']}"
    # The FK column points at requests; a spoken yes is not one.
    assert row["confirmed_by_request_id"] is None
    assert row["summary"] == "I ran a command in PowerShell: ipconfig"
    (started,) = events(con, "shell.started")
    (finished,) = events(con, "shell.finished")
    assert started["command"] == "ipconfig" and finished["exit_code"] == 0
    assert finished["output"] == "192.168.1.5\n"


def test_a_no_runs_nothing(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    turns, ctx = conversation(con, shell)
    call(ctx, command="Remove-Item C:\\Temp\\old -Recurse")
    turns.said("no, wait")
    assert "You said no" in call(ctx, command="Remove-Item C:\\Temp\\old -Recurse", confirm=True)
    assert shell.calls == []


def test_confirming_without_a_read_back_runs_nothing(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    turns, ctx = conversation(con, shell)
    turns.said("yes")
    assert "haven't read that back" in call(ctx, command="ipconfig", confirm=True)
    assert shell.calls == []


def test_a_different_command_than_the_one_read_back_runs_nothing(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    turns, ctx = conversation(con, shell)
    call(ctx, command="ipconfig")
    turns.said("yes")
    assert "haven't read that back" in call(ctx, command="ipconfig /release", confirm=True)
    assert shell.calls == []


def test_a_click_in_the_tools_tab_is_the_yes(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    _, ctx = conversation(con, shell, channel="cli", **{DIRECT_HUMAN: True})
    reply = call(ctx, command="ipconfig", confirm=True)
    assert isinstance(reply, Reply) and shell.calls == [("ipconfig", 60)]


def test_a_long_script_is_summarised_aloud_and_logged_in_full(con: sqlite3.Connection) -> None:
    shell = FakeShell(con)
    _, ctx = conversation(con, shell)
    script = "\n".join(f"Get-Item C:\\Folder{i}" for i in range(8))
    said = call(ctx, command=script)
    assert "an 8-line" not in said and "a 8-line script in PowerShell" in said
    assert FULL_TEXT_WHERE in said and "C:\\Folder7" not in said
    (proposed,) = events(con, "confirm.proposed")
    assert script in proposed["readback"]


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("echo hi" + chr(0x202E) + "exe.txt", "hidden characters"),
        ("powershell -enc SQBFAFgA", "encoded"),
        ("", "no command"),
    ],
)
def test_what_cannot_be_read_back_honestly_is_refused_before_it_is_proposed(
    con: sqlite3.Connection, command: str, why: str
) -> None:
    shell = FakeShell(con)
    _, ctx = conversation(con, shell)
    assert why in call(ctx, command=command)
    assert events(con, "confirm.proposed") == [] and shell.calls == []


# ───────────────────────────── secrets ─────────────────────────────


def test_no_secret_reaches_the_log_the_ledger_or_the_model(con: sqlite3.Connection) -> None:
    leaky = Outcome(
        exit_code=0,
        head=f"GEMINI={SECRET}\nANTHROPIC={SHAPED}\nok\n".encode(),
        total_bytes=200,
    )
    shell = FakeShell(con, leaky)
    turns, ctx = conversation(con, shell)
    command = f"Write-Output {SECRET}"
    said = call(ctx, command=command)
    turns.said("yes")
    reply = call(ctx, command=command, confirm=True)
    assert shell.calls[0][0] == command  # the approved text runs, unredacted
    for text in (said, reply.detail, everything_logged(con)):
        assert SECRET not in text and SHAPED not in text
    assert "[redacted" in reply.detail and "ok" in reply.detail


# ───────────────────────────── how it ended ─────────────────────────────


def _run_with(con: sqlite3.Connection, shell: FakeShell, **args: Any) -> str:
    turns, ctx = conversation(con, shell)
    call(ctx, command="ping example.com", **args)
    turns.said("yes")
    return call(ctx, command="ping example.com", confirm=True, **args)


def test_the_kill_switch_stops_a_running_command(con: sqlite3.Connection) -> None:
    seen: list[bool] = []

    def press_stop(should_stop: Callable[[], bool]) -> None:
        seen.append(should_stop())
        kill.bump_epoch(con, actor="test", reason="stop everything")
        seen.append(should_stop())

    stopped = Outcome(exit_code=None, stopped=True)
    reply = _run_with(con, FakeShell(con, stopped, during=press_stop))
    assert seen == [False, True]
    assert str(reply) == "I stopped the command because everything was told to stop."


@pytest.mark.parametrize(
    ("outcome", "sentence"),
    [
        (Outcome(exit_code=1, head=b"boom\n"), "The command failed with exit code 1."),
        (Outcome(exit_code=None, timed_out=True), "still running after 60 seconds"),
        (Outcome(exit_code=0), "The command finished and printed nothing."),
        (
            Outcome(exit_code=0, head=b"started\n", incomplete=True),
            "Something it started is still running.",
        ),
    ],
)
def test_the_summary_says_how_it_ended_and_nothing_more(
    con: sqlite3.Connection, outcome: Outcome, sentence: str
) -> None:
    assert sentence in str(_run_with(con, FakeShell(con, outcome)))


def test_a_shell_that_would_not_start_is_never_reported_as_run(con: sqlite3.Connection) -> None:
    said = _run_with(con, FakeShell(con, Outcome(exit_code=None, error="FileNotFoundError")))
    assert said == "I couldn't start PowerShell, so nothing ran."
    assert not isinstance(said, Reply)


@pytest.mark.parametrize(("given", "used"), [(99999, 600), (1, 5), ("ninety", 60), (120, 120)])
def test_the_timeout_is_bounded(con: sqlite3.Connection, given: Any, used: int) -> None:
    shell = FakeShell(con)
    _run_with(con, shell, timeout_s=given)
    assert shell.calls[0][1] == used


# ───────────────────────────── for real ─────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="runs /bin/sh")
def test_with_no_shell_handed_in_it_runs_this_machines_own(con: sqlite3.Connection) -> None:
    """The caller exists: the tool reaches jarvis.shell, not just a fake of it."""
    turns, ctx = conversation(con, None)
    ctx.extra.pop(SHELL)
    call(ctx, command="echo jarvis-$((40 + 2))")
    turns.said("yes")
    reply = call(ctx, command="echo jarvis-$((40 + 2))", confirm=True)
    assert isinstance(reply, Reply) and "jarvis-42" in reply.detail, reply


@pytest.mark.skipif(sys.platform != "win32", reason="runs real PowerShell")
def test_windows_the_tool_runs_powershell_hidden_and_reads_turkish(
    con: sqlite3.Connection,
) -> None:
    turns, ctx = conversation(con, None)
    ctx.extra.pop(SHELL)
    command = "Write-Output 'Türkçe: ğüşıöç'; cmd /c exit 4"
    call(ctx, command=command)
    turns.said("evet")
    reply = call(ctx, command=command, confirm=True)
    assert "Türkçe: ğüşıöç" in reply.detail and "exit code 4" in str(reply)


# ───────────────────────────── claude_code_status ─────────────────────────────


def status(con: sqlite3.Connection, facts: Any, channel: str = "desk") -> str:
    probe = facts if callable(facts) else (lambda: facts)
    ctx = ToolCtx(con=con, channel=channel, actor=channel, extra={CLAUDE_STATUS: probe})
    return Registry(computer.TOOLS).dispatch("claude_code_status", {}, ctx)


BASE = {"installed": True, "version": "2.1.273", "error": None}


def test_a_claude_account_is_named_with_the_email_at_the_desk(con: sqlite3.Connection) -> None:
    facts = {
        **BASE,
        "logged_in": True,
        "auth_method": "claude.ai",
        "subscription": "max",
        "email": "me@example.com",
        "org_name": "Home",
    }
    said = status(con, facts)
    assert isinstance(said, Reply)
    assert said.startswith("Claude Code 2.1.273 is signed in with your Claude Max account")
    assert "me@example.com" in said and "haven't tried a request" in said
    assert "me@example.com" in said.detail


def test_telegram_never_hears_the_email_or_organisation(con: sqlite3.Connection) -> None:
    facts = {**BASE, "logged_in": True, "auth_method": "claude.ai", "email": "me@example.com"}
    facts["org_name"] = "Secret Org"
    said = status(con, facts, channel="telegram")
    assert "me@example.com" not in said and "me@example.com" not in said.detail
    assert "Secret Org" not in said + said.detail
    assert "signed in" in said


@pytest.mark.parametrize(
    ("facts", "words"),
    [
        (
            {"logged_in": True, "auth_method": "api_key", "key_source": "ANTHROPIC_API_KEY"},
            "an API key from ANTHROPIC_API_KEY, so builds are billed to that key",
        ),
        ({"logged_in": True, "auth_method": "oauth_token"}, "long-lived sign-in token"),
        (
            {"logged_in": True, "auth_method": "third_party", "provider": "bedrock"},
            "set up to use Amazon Bedrock, not a Claude account",
        ),
        (
            {"logged_in": False, "auth_method": "none"},
            "installed but not signed in. Press Sign in to Claude Code",
        ),
        ({"installed": False, "logged_in": None}, "isn't installed on this computer"),
        (
            {"logged_in": None, "error": "the sign-in check did not answer in time"},
            "I couldn't tell whether Claude Code 2.1.273 is signed in",
        ),
    ],
)
def test_one_honest_sentence_per_state(
    con: sqlite3.Connection, facts: dict[str, Any], words: str
) -> None:
    assert words in status(con, {**BASE, **facts})


def test_unknown_is_never_said_as_signed_in(con: sqlite3.Connection) -> None:
    said = status(con, {**BASE, "logged_in": None, "auth_method": "claude.ai"})
    assert said.startswith("I couldn't tell whether") and "account" not in said


def test_a_probe_that_hands_over_more_than_it_should_is_filtered(con: sqlite3.Connection) -> None:
    facts = {**BASE, "logged_in": True, "auth_method": "oauth_token", "orgId": "org-123"}
    said = status(con, facts)
    assert "org-123" not in said + said.detail


def test_without_the_probe_or_with_a_broken_one_it_says_so(con: sqlite3.Connection) -> None:
    ctx = ToolCtx(con=con, channel="desk", actor="desk", extra={})
    said = Registry(computer.TOOLS).dispatch("claude_code_status", {}, ctx)
    assert said == "I can't check Claude Code from here."

    def boom() -> dict[str, Any]:
        raise RuntimeError("no")

    assert "the check itself failed" in status(con, boom)


def test_the_phone_cannot_ask(con: sqlite3.Connection) -> None:
    assert "isn't available over phone" in status(con, BASE, channel="phone")
