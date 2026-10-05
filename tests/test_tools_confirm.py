"""Read it back, then act only on the user's own yes — and a Reply the model says itself.

The model must never be able to approve its own proposal. Every refusal case
below is a way it could try: confirm before asking, confirm after a "no",
confirm with a different command than the one read back, confirm an hour later.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import effects
from jarvis.db import connect, migrate
from jarvis.tools import confirm
from jarvis.tools.confirm import (
    CONFIRMATIONS,
    DIRECT_HUMAN,
    HEARD_SINCE,
    MARK,
    Confirmations,
    TypedTurns,
    answer_in,
)
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Registry, Tool, ToolError
from jarvis.tools.reply import Reply

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def talk(
    con: sqlite3.Connection, *, channel: str = "desk", wait_s: float = 0.0
) -> tuple[TypedTurns, dict[str, Any], Clock]:
    """A conversation for tool tests: ``typed.said("yes")`` is the user answering.

    Returns (turns, extra, clock). Put ``extra`` in the ToolCtx; advance ``clock.t``
    to age a proposal. Reusable by any tool that needs a yes.
    """
    clock = Clock()
    turns = TypedTurns()
    box = Confirmations(wait_s=wait_s, clock=clock, sleep=lambda s: None)
    return turns, turns.keys(box), clock


def ctx_for(con: sqlite3.Connection, extra: dict[str, Any], channel: str = "desk") -> ToolCtx:
    return ToolCtx(con=con, channel=channel, actor=channel, extra=extra)


def doomsday(ctx: ToolCtx, target: str, confirm: bool = False) -> str:
    """A tool shaped like close_app and power: ask, then act on the yes."""
    key = target.strip().casefold()
    if not confirm:
        return confirm_ask(ctx, key, target)
    proposal = confirm_mod_granted(ctx, key)
    ctx.extra.setdefault("done", []).append((key, proposal.id))
    return f"Done: {target}."


def confirm_ask(ctx: ToolCtx, key: str, target: str) -> str:
    return confirm.ask(
        ctx, tool="doomsday", key=key, readback=f"I'll end {target}.", effect="pc.power"
    )


def confirm_mod_granted(ctx: ToolCtx, key: str) -> confirm.Proposal:
    return confirm.granted(ctx, tool="doomsday", key=key)


def events(con: sqlite3.Connection, kind: str) -> list[str]:
    return [r[0] for r in con.execute("SELECT payload FROM events WHERE kind=?", (kind,))]


# ───────────────────────────── yes and no ─────────────────────────────


@pytest.mark.parametrize(
    "words",
    ["yes", "Yes please.", "yeah go ahead", "do it", "sure", "OK", "evet", "Tamam, yap", "Olur"],
)
def test_a_yes_is_a_yes(words: str) -> None:
    assert answer_in(words) is True


@pytest.mark.parametrize(
    "words",
    [
        "no",
        "No, don't.",
        "yes — no, wait",
        "not yet",
        "cancel that",
        "hayır",
        "Hayir",
        "iptal et",
        "dur",
        "vazgeç",
    ],
)
def test_a_no_anywhere_wins(words: str) -> None:
    assert answer_in(words) is False


@pytest.mark.parametrize("words", ["", "  ", "huh", "what was that", "the second one"])
def test_neither_is_neither(words: str) -> None:
    assert answer_in(words) is None


# ───────────────────────────── the protocol ─────────────────────────────


def test_the_first_call_does_nothing_but_read_back(con: sqlite3.Connection) -> None:
    _, extra, _ = talk(con)
    ctx = ctx_for(con, extra)
    said = doomsday(ctx, "the computer")
    assert said == "I'll end the computer. Shall I go ahead? Say yes, or no."
    assert "done" not in extra
    assert len(events(con, "confirm.proposed")) == 1


def test_the_users_yes_after_the_read_back_lets_it_run_once(con: sqlite3.Connection) -> None:
    turns, extra, _ = talk(con)
    ctx = ctx_for(con, extra)
    turns.said("shut down the computer")
    doomsday(ctx, "the computer")
    turns.said("yes")
    assert doomsday(ctx, "the computer", confirm=True) == "Done: the computer."
    assert len(extra["done"]) == 1
    with pytest.raises(ToolError, match="haven't read that back"):
        doomsday(ctx, "the computer", confirm=True)  # one yes, one action
    assert len(extra["done"]) == 1


def test_confirming_without_asking_is_refused(con: sqlite3.Connection) -> None:
    turns, extra, _ = talk(con)
    turns.said("yes")  # said before anything was proposed
    with pytest.raises(ToolError, match="haven't read that back"):
        doomsday(ctx_for(con, extra), "the computer", confirm=True)
    assert "done" not in extra
    assert len(events(con, "confirm.refused")) == 1


def test_a_yes_said_before_the_read_back_does_not_count(con: sqlite3.Connection) -> None:
    # "Yes, shut it down" is the request, not the answer to the read-back.
    turns, extra, _ = talk(con)
    ctx = ctx_for(con, extra)
    turns.said("yes, shut it down")
    doomsday(ctx, "the computer")
    with pytest.raises(ToolError, match="didn't hear a yes"):
        doomsday(ctx, "the computer", confirm=True)
    assert "done" not in extra


def test_a_no_cancels_and_a_later_yes_does_not_revive_it(con: sqlite3.Connection) -> None:
    turns, extra, _ = talk(con)
    ctx = ctx_for(con, extra)
    doomsday(ctx, "the computer")
    turns.said("no, wait")
    with pytest.raises(ToolError, match="said no"):
        doomsday(ctx, "the computer", confirm=True)
    turns.said("actually yes")
    with pytest.raises(ToolError, match="haven't read that back"):
        doomsday(ctx, "the computer", confirm=True)
    assert "done" not in extra


def test_a_different_action_than_the_one_read_back_is_refused(con: sqlite3.Connection) -> None:
    turns, extra, _ = talk(con)
    ctx = ctx_for(con, extra)
    doomsday(ctx, "notepad")
    turns.said("yes")
    with pytest.raises(ToolError, match="haven't read that back"):
        doomsday(ctx, "the whole computer", confirm=True)
    assert "done" not in extra


def test_an_old_proposal_expires(con: sqlite3.Connection) -> None:
    turns, extra, clock = talk(con)
    ctx = ctx_for(con, extra)
    doomsday(ctx, "the computer")
    clock.t += confirm.EXPIRES_S + 1
    turns.said("yes")
    with pytest.raises(ToolError, match="haven't read that back"):
        doomsday(ctx, "the computer", confirm=True)


def test_no_way_to_hear_the_user_fails_closed(con: sqlite3.Connection) -> None:
    box = Confirmations(wait_s=0.0)
    ctx = ctx_for(con, {CONFIRMATIONS: box})
    doomsday(ctx, "the computer")
    with pytest.raises(ToolError, match="didn't hear a yes"):
        doomsday(ctx, "the computer", confirm=True)


def test_no_conversation_means_no_action_that_needs_a_yes(con: sqlite3.Connection) -> None:
    with pytest.raises(ToolError, match="can't ask you to confirm"):
        doomsday(ctx_for(con, {}), "the computer")


def test_a_press_of_the_button_is_the_yes(con: sqlite3.Connection) -> None:
    extra: dict[str, Any] = {CONFIRMATIONS: Confirmations(), DIRECT_HUMAN: True}
    assert doomsday(ctx_for(con, extra, "cli"), "notepad", confirm=True) == "Done: notepad."


def test_the_desk_waits_briefly_for_a_late_transcript(con: sqlite3.Connection) -> None:
    clock = Clock()
    heard: list[str] = []
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock.t += s
        heard.append("yes")  # the transcript lands while we wait

    box = Confirmations(wait_s=2.0, clock=clock, sleep=sleep)
    extra = {CONFIRMATIONS: box, MARK: lambda: 0, HEARD_SINCE: lambda mark: " ".join(heard)}
    ctx = ctx_for(con, extra)
    doomsday(ctx, "the computer")
    assert doomsday(ctx, "the computer", confirm=True) == "Done: the computer."
    assert slept == [0.1]


def test_the_read_back_is_logged_through_the_redactor(con: sqlite3.Connection) -> None:
    from jarvis.bus import Redactor

    secret = "sk-ant-" + "z" * 30
    turns, extra, _ = talk(con)
    extra["redactor"] = Redactor.of([secret])
    confirm.ask(
        ctx_for(con, extra),
        tool="run_command",
        key="x",
        readback=f"I'll run echo {secret}.",
        effect="shell.run",
    )
    (payload,) = events(con, "confirm.proposed")
    assert secret not in payload


# ───────────────────────────── the desk's transcript ─────────────────────────────


def test_the_desk_transcript_answers_only_after_the_mark_and_its_settle() -> None:
    from jarvis.voice.tools import Transcript

    clock = Clock()
    tr = Transcript(clock=clock, settle_s=0.75)
    tr.heard("shut it ")
    mark = tr.mark()
    clock.t += 0.3
    tr.heard("down")  # the tail of the request, transcribed late
    assert tr.since(mark) == ""
    clock.t += 2.0
    tr.heard("yes")
    assert tr.since(mark) == "yes"
    assert tr.since("not a mark") == ""


def test_the_desk_hands_every_tool_the_confirmation_keys(con: sqlite3.Connection) -> None:
    from jarvis.voice.tools import LiveTools, Transcript

    lt = LiveTools(
        registry=Registry(),
        open_db=lambda: con,
        transcript=Transcript(),
        confirmations=Confirmations(),
    )
    ctx = lt._ctx(con)
    assert {CONFIRMATIONS, MARK, HEARD_SINCE} <= set(ctx.extra)
    bare = LiveTools(registry=Registry(), open_db=lambda: con)
    assert CONFIRMATIONS not in bare._ctx(con).extra


# ───────────────────────────── Reply ─────────────────────────────


def test_a_reply_is_a_str_carrying_detail_for_the_model() -> None:
    import pickle

    r = Reply("That finished: exit 0.", detail="192.168.1.5")
    assert isinstance(r, str) and r == "That finished: exit 0." and r.detail == "192.168.1.5"
    assert r.aloud is False and Reply("x", aloud=True).aloud is True
    back = pickle.loads(pickle.dumps(r))
    assert back.detail == "192.168.1.5"


def test_the_desk_lets_the_model_say_a_reply_and_reads_the_rest(tmp_path: Path) -> None:
    import asyncio

    from jarvis.live.session import ToolCall
    from jarvis.voice.tools import LiveTools

    said: list[str] = []
    reg = Registry(
        [
            Tool("ip", "x", lambda: Reply("Done.", detail="192.168.1.5"), channels=("desk",)),
            Tool("plain", "x", lambda: "Opening Notepad.", channels=("desk",)),
        ]
    )
    path = tmp_path / "desk.db"
    first = connect(path)
    migrate(first)
    first.close()
    lt = LiveTools(registry=reg, open_db=lambda: connect(path), speak=lambda u: said.append(u.text))
    ip = asyncio.run(lt.dispatch(ToolCall(id="1", name="ip", args={})))
    assert said == [] and ip.scheduling == "WHEN_IDLE"
    assert ip.response["detail"] == "192.168.1.5" and ip.response["said"] == "Done."
    plain = asyncio.run(lt.dispatch(ToolCall(id="2", name="plain", args={})))
    assert said == ["Opening Notepad."] and plain.scheduling == "SILENT"
    assert "detail" not in plain.response


def test_the_text_chat_hands_a_replys_detail_to_the_model() -> None:
    src = (ROOT / "jarvis/live/chat.py").read_text(encoding="utf-8")
    assert 'response["detail"] = detail' in src


# ───────────────────────────── Tool.effect ─────────────────────────────


def test_a_tool_cannot_register_an_effect_nobody_classified() -> None:
    with pytest.raises(KeyError, match="no reversibility class"):
        Registry([Tool("x", "x", lambda: "", channels=("desk",), effect="pc.teleport")])
    Registry([Tool("x", "x", lambda: "", channels=("desk",), effect="pc.open")])


def test_the_new_kinds_are_classified_as_the_design_says() -> None:
    want = {
        "pc.open": "reversible",
        "pc.volume": "reversible",
        "pc.media": "reversible",
        "pc.lock": "reversible",
        "pc.app_close": "compensatable",
        "pc.power": "compensatable",
        "pc.sleep": "compensatable",
        "shell.run": "irreversible",
        "capture.vision": "irreversible",
    }
    assert {k: effects.reversibility_of(k) for k in want} == want
    assert "shell.run" in effects.IRREVERSIBILITY_REASONS
    assert "capture.vision" in effects.IRREVERSIBILITY_REASONS


# ───────────────────────────── the callers exist ─────────────────────────────


def _fn(name: str) -> str:
    tree = ast.parse((ROOT / "jarvis/__main__.py").read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(node)


def test_the_desk_gives_its_tools_a_way_to_hear_a_yes() -> None:
    assert "confirmations=Confirmations()" in _fn("_build_desk")


def test_both_chats_record_what_the_user_typed_before_the_model_sees_it() -> None:
    for name in ("cmd_chat", "_window_chat"):
        src = _fn(name)
        assert "TypedTurns()" in src and "typed.said(text)" in src, name
        assert "typed.keys(" in src, name


def test_the_tools_tab_press_is_the_yes() -> None:
    src = (ROOT / "jarvis/window/server.py").read_text(encoding="utf-8")
    assert "DIRECT_HUMAN: True" in src
