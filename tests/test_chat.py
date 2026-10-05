"""Jarvis by text: the Gemini API, the registry's tools, and our own tool loop.

Driven with REAL google-genai types and a fake transport, so the declarations,
the function responses and the history are validated by the SDK's own models
rather than by what this file believes they look like.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("google.genai")

from google.genai import types as t  # noqa: E402

from jarvis import __main__ as cli  # noqa: E402
from jarvis import secrets  # noqa: E402
from jarvis.db import connect, migrate  # noqa: E402
from jarvis.live.chat import PERSONA, GeminiChat, persona  # noqa: E402
from jarvis.live.text import GeminiSearch, TextCallFailed  # noqa: E402
from jarvis.tools.ctx import ToolCtx  # noqa: E402
from jarvis.tools.default import registry  # noqa: E402
from jarvis.tools.registry import Registry, Tool  # noqa: E402


def text_reply(text: str) -> t.GenerateContentResponse:
    return t.GenerateContentResponse(
        candidates=[t.Candidate(content=t.Content(role="model", parts=[t.Part(text=text)]))]
    )


def call_reply(*calls: tuple[str, dict[str, Any]]) -> t.GenerateContentResponse:
    parts = [
        t.Part(
            function_call=t.FunctionCall(id=f"c{i}", name=name, args=args),
            thought_signature=b"opaque-signature",
        )
        for i, (name, args) in enumerate(calls)
    ]
    return t.GenerateContentResponse(
        candidates=[t.Candidate(content=t.Content(role="model", parts=parts))]
    )


class FakeModels:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        snapshot = contents if isinstance(contents, str) else list(contents)
        self.calls.append({"model": model, "contents": snapshot, "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    def __init__(self, replies: list[Any]) -> None:
        self.models = FakeModels(replies)


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


def make_chat(con: sqlite3.Connection, replies: list[Any], **kw: Any) -> GeminiChat:
    reg = registry()
    ctx = ToolCtx(con=con, channel="cli", actor="cli", extra=kw.pop("extra", {}))
    return GeminiChat(
        api_key="k",
        declarations=reg.declarations("cli"),
        dispatch=lambda name, args: reg.dispatch(name, args, ctx),
        client=FakeClient(replies),
        **kw,
    )


def test_every_registry_declaration_is_a_valid_sdk_declaration(con: sqlite3.Connection) -> None:
    chat = make_chat(con, [])
    config = chat._config(tools=True)
    names = {d.name for d in config.tools[0].function_declarations}
    assert {"weather", "remind_me", "web_search", "remember", "correct_hearing"} <= names
    # Ours, not the SDK's: dispatch has to go through the registry's gates.
    assert config.automatic_function_calling.disable is True


def test_a_plain_question_gets_a_plain_answer(con: sqlite3.Connection) -> None:
    chat = make_chat(con, [text_reply("Paris.")])
    out = chat.send("capital of France?")
    assert out.text == "Paris." and out.tools == ()
    assert [c.role for c in chat.history] == ["user", "model"]


def test_a_tool_call_runs_through_the_registry_and_the_answer_follows(
    con: sqlite3.Connection,
) -> None:
    chat = make_chat(
        con,
        [call_reply(("remember", {"fact": "my locker is 214"})), text_reply("Noted.")],
    )
    out = chat.send("remember my locker is 214")
    assert out.text == "Noted."
    assert out.tools == (("remember", "I'll remember that: my locker is 214"),)
    assert con.execute("SELECT text FROM notes").fetchone()[0] == "my locker is 214"

    second = chat.client.models.calls[1]["contents"]
    model_turn, response_turn = second[-2], second[-1]
    # The model's turn goes back UNCHANGED: the thought signature survives.
    assert model_turn.parts[0].thought_signature == b"opaque-signature"
    fr = response_turn.parts[0].function_response
    assert response_turn.role == "user" and fr.id == "c0" and fr.name == "remember"
    assert fr.response == {"result": "I'll remember that: my locker is 214"}


def test_parallel_calls_are_all_answered_in_one_turn(con: sqlite3.Connection) -> None:
    chat = make_chat(
        con,
        [
            call_reply(("remember", {"fact": "a"}), ("remember", {"fact": "b"})),
            text_reply("Both noted."),
        ],
    )
    out = chat.send("remember a and b")
    assert len(out.tools) == 2
    assert len(chat.client.models.calls[1]["contents"][-1].parts) == 2


def test_the_channel_gate_still_applies(con: sqlite3.Connection) -> None:
    # A model that calls a tool its channel may not use is refused by the
    # registry, not run — the reason the SDK's automatic calling is off.
    ran: list[str] = []
    reg = Registry(
        [
            Tool(
                name="push_to_main",
                description="d",
                handler=lambda: ran.append("x") or "pushed",
                channels=("desk",),
            )
        ]
    )
    ctx = ToolCtx(con=con, channel="cli", actor="cli")
    chat = GeminiChat(
        api_key="k",
        declarations=reg.declarations("cli"),
        dispatch=lambda name, args: reg.dispatch(name, args, ctx),
        client=FakeClient([call_reply(("push_to_main", {})), text_reply("ok")]),
    )
    out = chat.send("push it")
    assert ran == [] and "isn't available over cli" in out.tools[0][1]
    assert chat._config(tools=True).tools is None  # and it was never offered


def test_a_model_that_keeps_calling_tools_is_made_to_answer(con: sqlite3.Connection) -> None:
    loop = [call_reply(("recall", {})) for _ in range(3)]
    chat = make_chat(con, [*loop, text_reply("Here's what I have.")], max_rounds=3)
    out = chat.send("what do you know")
    assert out.text == "Here's what I have."
    last = chat.client.models.calls[-1]["config"]
    assert last.tools is None  # the final round offers nothing to call


def test_a_failed_request_leaves_the_history_clean_for_a_retry(con: sqlite3.Connection) -> None:
    chat = make_chat(con, [RuntimeError("503"), text_reply("Hello.")])
    with pytest.raises(TextCallFailed, match="503"):
        chat.send("hi")
    assert chat.history == []
    assert chat.send("hi").text == "Hello."


def test_trimming_never_leaves_an_orphaned_function_response(con: sqlite3.Connection) -> None:
    replies: list[Any] = []
    for _ in range(6):
        replies += [call_reply(("recall", {})), text_reply("ok")]
    chat = make_chat(con, replies, max_history=5)
    for _ in range(6):
        chat.send("again")
    first = chat.history[0]
    assert first.role == "user" and not any(p.function_response for p in first.parts)


def test_an_empty_answer_says_why(con: sqlite3.Connection) -> None:
    blocked = t.GenerateContentResponse(
        candidates=[t.Candidate(content=None, finish_reason=t.FinishReason.SAFETY)]
    )
    assert "SAFETY" in make_chat(con, [blocked]).send("x").text


def test_the_persona_is_general_and_carries_the_users_notes() -> None:
    assert "general-purpose" in PERSONA and "Claude Code" in PERSONA
    assert persona(extra="- my locker is 214").endswith("- my locker is 214")


# ───────────────────────────── search ─────────────────────────────


def test_search_is_grounded_and_names_its_sources() -> None:
    reply = t.GenerateContentResponse(
        candidates=[
            t.Candidate(
                content=t.Content(role="model", parts=[t.Part(text="Galatasaray won 2-1.")]),
                grounding_metadata=t.GroundingMetadata(
                    grounding_chunks=[
                        t.GroundingChunk(web=t.GroundingChunkWeb(title="bbc.com", uri="u")),
                        t.GroundingChunk(web=t.GroundingChunkWeb(title="bbc.com", uri="u2")),
                        t.GroundingChunk(web=t.GroundingChunkWeb(title="espn.com", uri="u3")),
                    ]
                ),
            )
        ]
    )
    client = FakeClient([reply])
    said = GeminiSearch(api_key="k", client=client)("who won")
    assert said == "Galatasaray won 2-1. (Sources: bbc.com, espn.com.)"
    config = client.models.calls[0]["config"]
    assert config.tools[0].google_search is not None
    assert "who won" in client.models.calls[0]["contents"]


def test_a_failed_search_is_a_text_call_failure() -> None:
    with pytest.raises(TextCallFailed):
        GeminiSearch(api_key="k", client=FakeClient([RuntimeError("quota")]))("x")


# ───────────────────────────── the command ─────────────────────────────


def test_chat_one_shot_from_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for s in secrets.SECRETS:
        monkeypatch.delenv(s.env, raising=False)
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "k")
    fake = FakeClient([call_reply(("remember", {"fact": "coffee black"})), text_reply("Noted.")])
    import google.genai

    monkeypatch.setattr(google.genai, "Client", lambda api_key: fake)
    assert cli.main(["--db", str(tmp_path / "c.db"), "chat", "remember", "coffee", "black"]) == 0
    out = capsys.readouterr().out
    assert "[remember] I'll remember that: coffee black" in out and "Noted." in out
    sent = fake.models.calls[0]["config"]
    assert "general-purpose" in sent.system_instruction


def test_chat_without_a_key_says_how_to_set_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for s in secrets.SECRETS:
        monkeypatch.delenv(s.env, raising=False)
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: None)
    assert cli.main(["--db", str(tmp_path / "c.db"), "chat", "hi"]) == 2
    assert "gemini_api_key" in capsys.readouterr().err
