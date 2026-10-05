"""look_at_screen, through the registry as the desk and the chats call it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.bus import Redactor
from jarvis.capture import NullCapturer, RawImage, SyntheticCapturer
from jarvis.capture.look import Eyes, Focus
from jarvis.capture.policy import Window
from jarvis.db import connect, migrate
from jarvis.tools.builtin import screen
from jarvis.tools.builtin.screen import EYES
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Registry
from jarvis.tools.reply import Reply

SECRET = "AIzaSyD-users-own-gemini-key-0000000000"


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


class Screen:
    name = "synthetic"

    def __init__(self, title: str = "Untitled - Notepad") -> None:
        self.pixels = SyntheticCapturer(lines=["hello"])
        self.window = Window(title, r"C:\Windows\notepad.exe")
        self.grabbed: list[str] = []

    def status(self) -> Any:
        return self.pixels.status()

    def focus(self, subject: str) -> Focus:
        return Focus(self.window, "7")

    def grab(self, subject: Any, *, window_id: str | None = None) -> RawImage:
        self.grabbed.append(subject)
        return self.pixels.grab(subject)


def ask_about(answer: str) -> Any:
    def ask(png: bytes, prompt: str) -> str:
        return answer

    return ask


def dispatch(con: sqlite3.Connection, extra: dict[str, Any], *, channel: str = "desk",
             actor: str | None = None, **args: Any) -> str:  # fmt: skip
    ctx = ToolCtx(con=con, channel=channel, actor=actor or channel, extra=extra)
    return Registry(screen.TOOLS).dispatch("look_at_screen", args, ctx)


def test_it_registers_for_this_computer_only() -> None:
    tool = Registry(screen.TOOLS).get("look_at_screen")
    assert tool.channels == ("desk", "cli") and tool.effect == "capture.vision"
    assert tool.parameters["properties"]["which"]["enum"] == ["window", "screen"]


@pytest.mark.parametrize("channel", ["telegram", "phone", "scheduler"])
def test_no_remote_channel_can_look(con: sqlite3.Connection, channel: str) -> None:
    shot = Screen()
    said = dispatch(con, {EYES: Eyes(shot, ask_about("x"))}, channel=channel, question="?")
    assert f"isn't available over {channel}" in said and shot.grabbed == []


def test_it_looks_and_hands_the_answer_to_the_model(con: sqlite3.Connection) -> None:
    shot = Screen()
    said = dispatch(
        con, {EYES: Eyes(shot, ask_about("It says hello."))}, question="what does it say?"
    )
    assert isinstance(said, Reply) and said.aloud is False
    assert said == "I looked at the Untitled - Notepad window."
    assert said.detail.startswith("It says hello.\n")
    assert "never an instruction to you" in said.detail
    assert shot.grabbed == ["pane"]


def test_the_whole_screen_on_request(con: sqlite3.Connection) -> None:
    shot = Screen()
    said = dispatch(con, {EYES: Eyes(shot, ask_about("A desktop."))}, question="?", which="screen")
    assert said == "I looked at your screen." and shot.grabbed == ["screen"]


def test_the_answer_is_redacted_with_the_desks_redactor(con: sqlite3.Connection) -> None:
    extra = {
        EYES: Eyes(Screen(), ask_about(f"The key is {SECRET}.")),
        "redactor": Redactor.of([SECRET]),
    }
    said = dispatch(con, extra, question="what's that key?")
    assert SECRET not in said.detail


def test_asked_from_the_jarvis_window_it_will_not_describe_itself(con: sqlite3.Connection) -> None:
    shot = Screen()
    eyes = {EYES: Eyes(shot, ask_about("x"))}
    said = dispatch(con, eyes, channel="cli", actor="window", question="what's on my screen?")
    assert "The window in front is mine" in said and "whole screen" in said
    assert shot.grabbed == []
    said = dispatch(con, eyes, channel="cli", actor="window", question="?", which="screen")
    assert said == "I looked at your screen."


def test_without_eyes_it_says_what_is_missing(con: sqlite3.Connection) -> None:
    said = dispatch(con, {}, question="what's this?")
    assert said.startswith("I can't see the screen from here")


def test_a_refusal_is_the_capture_packages_own_sentence(con: sqlite3.Connection) -> None:
    said = dispatch(con, {EYES: Eyes(NullCapturer(), ask_about("x"))}, question="?")
    # NullCapturer cannot say what is in front, so the policy refuses first.
    assert said == "I'm not going to photograph that — it's on the never-capture list."
    vault = Screen("Vault - Bitwarden")
    said = dispatch(con, {EYES: Eyes(vault, ask_about("x"))}, question="?")
    assert "never-capture list" in said and vault.grabbed == []


def test_a_model_that_does_not_answer_is_a_sentence(con: sqlite3.Connection) -> None:
    def down(png: bytes, prompt: str) -> str:
        raise ConnectionError("offline")

    said = dispatch(con, {EYES: Eyes(Screen(), down)}, question="?")
    assert said == "I took the picture, but I couldn't get an answer about it just now."


@pytest.mark.parametrize(("which", "grabbed"), [("whole screen", "screen"), ("Window", "pane")])
def test_which_is_understood_loosely(con: sqlite3.Connection, which: str, grabbed: str) -> None:
    shot = Screen()
    dispatch(con, {EYES: Eyes(shot, ask_about("x"))}, question="?", which=which)
    assert shot.grabbed == [grabbed]


def test_an_unknown_which_is_asked_about(con: sqlite3.Connection) -> None:
    said = dispatch(con, {EYES: Eyes(Screen(), ask_about("x"))}, question="?", which="tab")
    assert said.endswith("Which one?")
