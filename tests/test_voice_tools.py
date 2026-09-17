"""The user's own words, two permission tables, and who speaks the answer.

These are the three places the desk seam can be wrong without anything failing:
a build assembled from Gemini's paraphrase (every containment check still
passes), a tool offered on a leg that never listed it (nothing raises), and a
tool result nobody says out loud (the tool ran; the user heard silence).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.db import connect, migrate
from jarvis.live.profiles import AGENT_CALL, DESK, PHONE_USER, SessionProfile
from jarvis.live.session import ToolCall
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.default import registry
from jarvis.tools.registry import Registry, Tool
from jarvis.voice.router import Utterance
from jarvis.voice.tools import LiveTools, Transcript


@pytest.fixture
def dbpath(tmp_path: Path) -> Iterator[Path]:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    c.close()
    yield p


def opener(path: Path):
    return lambda: connect(path)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ───────────────────────────── the transcript ─────────────────────────────


def test_fragments_join_into_one_sentence() -> None:
    t = Transcript()
    for part in ("let's build ", "an app that ", "watches my comments"):
        t.heard(part)
    assert t.words() == "let's build an app that watches my comments"


def test_whitespace_is_folded_so_containment_is_not_defeated_by_a_newline() -> None:
    t = Transcript()
    t.heard("no  Docker\n\n and  no auth")
    assert t.words() == "no Docker and no auth"


def test_speech_older_than_the_window_falls_out() -> None:
    clock = FakeClock()
    t = Transcript(window_s=60.0, clock=clock)
    t.heard("something about tax returns")
    clock.advance(61.0)
    t.heard("let's build a comment watcher")
    assert t.words() == "let's build a comment watcher"


def test_a_request_spanning_two_turns_is_kept_whole() -> None:
    """The reason the window is not 'the current turn'."""
    clock = FakeClock()
    t = Transcript(window_s=180.0, clock=clock)
    t.heard("let's build a comment watcher")
    clock.advance(20.0)
    t.heard(" oh and no Docker")
    assert "comment watcher" in t.words()
    assert "no Docker" in t.words()


def test_the_window_is_bounded_by_characters_too() -> None:
    t = Transcript(max_chars=40)
    for i in range(20):
        t.heard(f"sentence number {i}. ")
    assert len(t.words()) <= 60  # trimmed, plus the last fragment
    assert "19" in t.words()


def test_an_empty_fragment_is_not_a_word() -> None:
    t = Transcript()
    t.heard("")
    assert not t
    t.heard("hello")
    assert t


def test_clearing_forgets_everything() -> None:
    t = Transcript()
    t.heard("build me a thing")
    t.clear()
    assert t.words() == ""


# ───────────────────────────── the two tables ─────────────────────────────


def a_tool(name: str, channels: tuple[str, ...] = ("desk", "telegram", "phone", "cli")) -> Tool:
    return Tool(name=name, description=name, handler=lambda: name, channels=channels)


def test_a_tool_must_pass_both_tables(dbpath: Path) -> None:
    reg = Registry([a_tool("code_build"), a_tool("secret_tool")])
    lt = LiveTools(registry=reg, open_db=opener(dbpath))
    profile = SessionProfile(name="x", tools=("code_build", "never_built"))
    assert [d["name"] for d in lt.declarations(profile)] == ["code_build"]


def test_a_tool_this_channel_may_not_call_is_never_offered(dbpath: Path) -> None:
    reg = Registry([a_tool("code_build", channels=("desk",))])
    profile = SessionProfile(name="x", tools=("code_build",))
    on_phone = LiveTools(registry=reg, open_db=opener(dbpath), channel="phone")
    assert on_phone.declarations(profile) == []
    at_desk = LiveTools(registry=reg, open_db=opener(dbpath), channel="desk")
    assert [d["name"] for d in at_desk.declarations(profile)] == ["code_build"]


def test_the_third_party_leg_is_offered_nothing_we_ship(dbpath: Path) -> None:
    """AGENT_CALL's surface is two tools it does not have. It must stay empty."""
    lt = LiveTools(registry=registry(), open_db=opener(dbpath), channel="phone")
    assert lt.declarations(AGENT_CALL) == []


def test_drift_between_a_profile_and_the_tools_is_reportable(dbpath: Path) -> None:
    lt = LiveTools(registry=registry(), open_db=opener(dbpath), channel="desk")
    # Named in the profile, not built yet. Silent today; printed by `doctor`.
    assert "answer_question" in lt.unresolved(DESK)
    assert "code_build" not in lt.unresolved(DESK)
    # And the other direction is empty, so every desk tool is actually reachable.
    assert lt.unreachable(DESK) == ()


def test_the_phone_profile_does_not_offer_a_build(dbpath: Path) -> None:
    lt = LiveTools(registry=registry(), open_db=opener(dbpath), channel="phone")
    assert "code_build" not in [d["name"] for d in lt.declarations(PHONE_USER)]
    assert "project_status" in [d["name"] for d in lt.declarations(PHONE_USER)]


# ───────────────────────────── dispatch ─────────────────────────────


async def test_the_handler_sees_the_users_words_not_the_models(dbpath: Path) -> None:
    seen: list[str] = []

    def handler(ctx: ToolCtx, summary: str = "") -> str:
        seen.append(str(ctx.extra.get("transcript", "")))
        return "filed"

    reg = Registry([Tool(name="code_build", description="d", handler=handler)])
    t = Transcript()
    t.heard("build me a comment watcher with no Docker")
    lt = LiveTools(registry=reg, open_db=opener(dbpath), transcript=t)

    await lt.dispatch(ToolCall(id="1", name="code_build", args={"summary": "a bot, best practice"}))
    assert seen == ["build me a comment watcher with no Docker"]


async def test_each_call_gets_its_own_connection_and_closes_it(dbpath: Path) -> None:
    handles: list[sqlite3.Connection] = []
    opened: list[sqlite3.Connection] = []

    def open_one() -> sqlite3.Connection:
        con = connect(dbpath)
        opened.append(con)
        return con

    def handler(ctx: ToolCtx) -> str:
        handles.append(ctx.con)
        return "ok"

    lt = LiveTools(
        registry=Registry([Tool(name="t", description="d", handler=handler)]), open_db=open_one
    )
    await lt.dispatch(ToolCall(id="1", name="t"))
    await lt.dispatch(ToolCall(id="2", name="t"))
    assert len(opened) == 2
    assert handles[0] is not handles[1]
    for con in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")


async def test_with_a_reader_the_model_is_told_silently(dbpath: Path) -> None:
    said: list[Utterance] = []
    reg = Registry([Tool(name="t", description="d", handler=lambda: "Right — comment watcher.")])
    lt = LiveTools(registry=reg, open_db=opener(dbpath), speak=said.append)

    result = await lt.dispatch(ToolCall(id="1", name="t"))
    assert result.scheduling == "SILENT"
    assert result.response["already_spoken"] is True
    assert said[0].text == "Right — comment watcher."
    # Not 'exact' — these are Jarvis's own sentences — but never 'free' either:
    # they carry promises a paraphrase can drop.
    assert said[0].fidelity == "faithful"


async def test_with_no_reader_the_model_says_it_rather_than_nobody(dbpath: Path) -> None:
    reg = Registry([Tool(name="t", description="d", handler=lambda: "Right — comment watcher.")])
    lt = LiveTools(registry=reg, open_db=opener(dbpath))
    result = await lt.dispatch(ToolCall(id="1", name="t"))
    assert result.scheduling == "WHEN_IDLE"
    assert result.response["already_spoken"] is False
    assert result.response["said"] == "Right — comment watcher."


async def test_an_async_reader_is_awaited(dbpath: Path) -> None:
    said: list[Utterance] = []

    async def speak(utt: Utterance) -> None:
        said.append(utt)

    reg = Registry([Tool(name="t", description="d", handler=lambda: "hello")])
    lt = LiveTools(registry=reg, open_db=opener(dbpath), speak=speak)
    await lt.dispatch(ToolCall(id="1", name="t"))
    assert [u.text for u in said] == ["hello"]


async def test_a_crashing_tool_comes_back_as_a_sentence(dbpath: Path) -> None:
    def boom() -> str:
        raise RuntimeError("the socket went away")

    reg = Registry([Tool(name="t", description="d", handler=boom)])
    lt = LiveTools(registry=reg, open_db=opener(dbpath))
    result = await lt.dispatch(ToolCall(id="1", name="t"))
    assert "the socket went away" in result.response["said"]


async def test_extra_context_reaches_the_handler_but_never_shadows_the_transcript(
    dbpath: Path,
) -> None:
    seen: list[dict] = []
    reg = Registry([Tool(name="t", description="d", handler=lambda ctx: seen.append(ctx.extra))])
    t = Transcript()
    t.heard("the real words")
    lt = LiveTools(
        registry=reg,
        open_db=opener(dbpath),
        transcript=t,
        extra={"spend_threshold_usd": 42.0, "transcript": "a planted paraphrase"},
    )
    await lt.dispatch(ToolCall(id="1", name="t"))
    assert seen[0]["spend_threshold_usd"] == 42.0
    assert seen[0]["transcript"] == "the real words"


async def test_a_real_build_goes_all_the_way_to_a_row(dbpath: Path) -> None:
    """The whole hop, through the adapter the desk actually uses."""
    t = Transcript()
    t.heard("let's build a comment watcher, use Opus with max effort, and no Docker")
    lt = LiveTools(registry=registry(), open_db=opener(dbpath), transcript=t)

    result = await lt.dispatch(
        ToolCall(id="1", name="code_build", args={"project_name": "comment watcher"})
    )
    assert "comment watcher" in result.response["said"]

    con = connect(dbpath)
    try:
        row = con.execute("SELECT * FROM jobs").fetchone()
        assert row["kind"] == "repo_setup"
        assert row["model"] == "opus"
        assert row["effort"] == "max"
        assert "no Docker" in row["prompt_text"]
    finally:
        con.close()
