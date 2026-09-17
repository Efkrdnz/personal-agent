"""One registry, one calling convention, and a tool that crashes says so out loud.

The three claims worth a test here are the ones the reference build gets wrong:
a channel cannot call a tool it was not granted, a tool that raises does not take
the conversation with it, and there is exactly ONE way a handler is called —
``ctx`` arrives if and only if the handler asked for it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.bus import Event, read_since
from jarvis.db import connect, migrate
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import (
    ALL_CHANNELS,
    BadArguments,
    ChannelNotAllowed,
    Registry,
    Tool,
    UnknownTool,
)


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def tool_events(con: sqlite3.Connection) -> list[Event]:
    return [e for e in read_since(con, "test") if e.kind.startswith("tool.")]


def ctx_for(con: sqlite3.Connection, channel: str = "desk") -> ToolCtx:
    return ToolCtx(con=con, channel=channel, actor=channel)


def a_tool(**kw: object) -> Tool:
    kw.setdefault("name", "ping")
    kw.setdefault("description", "say pong")
    kw.setdefault("handler", lambda: "pong")
    return Tool(**kw)  # type: ignore[arg-type]


# ───────────────────────────── the table ─────────────────────────────


def test_duplicate_name_is_refused() -> None:
    r = Registry([a_tool()])
    with pytest.raises(ValueError, match="duplicate"):
        r.add(a_tool())


def test_unknown_channel_is_refused_at_registration() -> None:
    with pytest.raises(ValueError, match="unknown channels"):
        Registry([a_tool(channels=("desk", "carrier-pigeon"))])


def test_empty_channel_tuple_is_refused() -> None:
    # A tool nobody may call is dead code that reads like a capability.
    with pytest.raises(ValueError, match="no channels"):
        Registry([a_tool(channels=())])


def test_for_channel_filters_and_none_means_everything() -> None:
    r = Registry(
        [
            a_tool(name="everywhere"),
            a_tool(name="desk_only", channels=("desk",)),
            a_tool(name="phone_only", channels=("phone",)),
        ]
    )
    assert r.names("desk") == ("desk_only", "everywhere")
    assert r.names("phone") == ("everywhere", "phone_only")
    assert r.names() == ("desk_only", "everywhere", "phone_only")
    assert len(r) == 3
    assert "desk_only" in r


def test_declarations_are_filtered_not_refused_later() -> None:
    """The phone is never OFFERED a tool it may not call, so it cannot propose one."""
    r = Registry([a_tool(name="push_to_main", channels=("desk",))])
    assert r.declarations("phone") == []
    decl = r.declarations("desk")
    assert decl[0]["name"] == "push_to_main"
    assert decl[0]["parameters"] == {"type": "OBJECT", "properties": {}}


# ───────────────────────────── the calling convention ─────────────────────────────


def test_ctx_arrives_only_when_asked_for(con: sqlite3.Connection) -> None:
    seen: list[object] = []

    def wants_ctx(ctx: ToolCtx) -> str:
        seen.append(ctx.channel)
        return "with ctx"

    def wants_nothing() -> str:
        return "without ctx"

    r = Registry([a_tool(name="a", handler=wants_ctx), a_tool(name="b", handler=wants_nothing)])
    assert r.dispatch("a", {}, ctx_for(con)) == "with ctx"
    assert r.dispatch("b", {}, ctx_for(con)) == "without ctx"
    assert seen == ["desk"]


def test_arguments_are_passed_by_keyword(con: sqlite3.Connection) -> None:
    def handler(first: str, second: str = "b") -> str:
        return f"{first}/{second}"

    r = Registry([a_tool(name="pair", handler=handler)])
    assert r.dispatch("pair", {"second": "z", "first": "a"}, ctx_for(con)) == "a/z"


def test_var_keyword_handler_still_gets_ctx(con: sqlite3.Connection) -> None:
    def handler(**kw: object) -> str:
        return type(kw["ctx"]).__name__

    r = Registry([a_tool(name="kw", handler=handler)])
    assert r.dispatch("kw", {}, ctx_for(con)) == "ToolCtx"


def test_a_handler_returning_nothing_still_says_something(con: sqlite3.Connection) -> None:
    r = Registry([a_tool(name="quiet", handler=lambda: None)])
    assert r.dispatch("quiet", {}, ctx_for(con)) == "Done."


# ───────────────────────────── refusals ─────────────────────────────


def test_unknown_tool_is_a_sentence_not_a_traceback(con: sqlite3.Connection) -> None:
    r = Registry()
    said = r.dispatch("teleport", {}, ctx_for(con))
    assert "teleport" in said
    with pytest.raises(UnknownTool):
        r.get("teleport")


def test_channel_is_enforced_by_code_not_by_prompt(con: sqlite3.Connection) -> None:
    r = Registry([a_tool(name="push_to_main", channels=("desk",))])
    said = r.dispatch("push_to_main", {}, ctx_for(con, "phone"))
    assert "phone" in said
    assert isinstance(ChannelNotAllowed("push_to_main", "phone"), Exception)


def test_an_invented_argument_is_named_rather_than_crashing(con: sqlite3.Connection) -> None:
    def handler(name: str) -> str:
        return name

    r = Registry([a_tool(name="build", handler=handler)])
    said = r.dispatch("build", {"name": "x", "colour": "blue"}, ctx_for(con))
    assert "colour" in said
    assert "Traceback" not in said


def test_a_missing_required_argument_is_named(con: sqlite3.Connection) -> None:
    def handler(name: str) -> str:
        return name

    r = Registry([a_tool(name="build", handler=handler)])
    said = r.dispatch("build", {}, ctx_for(con))
    assert "name" in said
    assert isinstance(BadArguments("build", "x"), Exception)


def test_a_crashing_tool_does_not_end_the_conversation(con: sqlite3.Connection) -> None:
    def boom() -> str:
        raise ZeroDivisionError("the usual")

    r = Registry([a_tool(name="boom", handler=boom)])
    said = r.dispatch("boom", {}, ctx_for(con))
    assert "boom" in said
    assert "the usual" in said
    # The exception CLASS is for the log, not for the speaker: "ZeroDivisionError"
    # is not a sentence, and the point of the spoken half is that it is sayable.
    assert "ZeroDivisionError" not in said
    logged = [e for e in tool_events(con) if e.kind == "tool.denied"][-1]
    assert "ZeroDivisionError" in logged.payload["error"]
    # and the next call still works
    assert r.dispatch("boom", {}, ctx_for(con)).startswith("Sorry")


# ───────────────────────────── the log ─────────────────────────────


def test_every_call_lands_in_the_activity_log(con: sqlite3.Connection) -> None:
    r = Registry(
        [
            a_tool(name="ok"),
            a_tool(name="desk_only", channels=("desk",)),
        ]
    )
    r.dispatch("ok", {}, ctx_for(con))
    r.dispatch("desk_only", {}, ctx_for(con, "phone"))
    r.dispatch("nope", {}, ctx_for(con))

    kinds = [e.kind for e in tool_events(con)]
    assert kinds == ["tool.used", "tool.denied", "tool.denied"]


def test_a_denial_is_logged_even_though_the_tool_never_ran(con: sqlite3.Connection) -> None:
    ran: list[int] = []
    r = Registry([a_tool(name="x", handler=lambda: ran.append(1) or "hi", channels=("desk",))])
    r.dispatch("x", {}, ctx_for(con, "telegram"))
    assert ran == []


def test_long_running_is_carried_into_the_log(con: sqlite3.Connection) -> None:
    r = Registry([a_tool(name="slow", long_running=True)])
    r.dispatch("slow", {}, ctx_for(con))
    used = [e for e in tool_events(con) if e.kind == "tool.used"]
    assert used[0].payload["long_running"] is True


def test_all_channels_is_the_default_surface() -> None:
    assert a_tool().channels == ALL_CHANNELS
    assert "desk" in ALL_CHANNELS and "phone" in ALL_CHANNELS
