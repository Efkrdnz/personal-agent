"""Answering by voice: an index in, the frozen array's own label out.

The claim worth testing is a NEGATIVE one — there is no path from a string the
model produced to the answer that gets stored. A model that mishears "Postgres"
as "PostgreSQL", or improves an option on its way past, cannot change what the
user agreed to, because the only thing it can pass is a number.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import requests as rq
from jarvis.cc import gate
from jarvis.db import connect, migrate
from jarvis.tools.builtin import answer as ansmod
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.default import registry


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


ONE = {
    "questions": [
        {
            "question": "Which database?",
            "options": [
                {"label": "SQLite", "description": "one file"},
                {"label": "Postgres", "description": "a server"},
            ],
        }
    ]
}
MULTI = {
    "questions": [
        {
            "question": "Which features?",
            "multiSelect": True,
            "options": [{"label": "Due dates"}, {"label": "Tags"}, {"label": "Priorities"}],
        }
    ]
}


def ask(con: sqlite3.Connection, payload: dict = ONE, tool: str = "AskUserQuestion") -> rq.Request:
    return gate.ensure_request(
        con,
        tool_name=tool,
        input_data=payload,
        job_id=None,
        tool_use_id=f"toolu_{id(payload)}_{tool}",
        actor="runner",
    )


def ctx_for(
    con: sqlite3.Connection, req: rq.Request | None = None, channel: str = "desk"
) -> ToolCtx:
    extra = {ansmod.OPEN_QUESTION: req.id} if req else {}
    return ToolCtx(con=con, channel=channel, actor=channel, extra=extra)


# ───────────────────────────── the index rule ─────────────────────────────


def test_the_number_resolves_to_the_frozen_arrays_own_label(con: sqlite3.Connection) -> None:
    req = ask(con)
    said = ansmod.answer_question(ctx_for(con, req), option=2)
    assert "Postgres" in said
    fresh = rq.get_request(con, req.id)
    assert fresh.answer == {
        "answers": {"Which database?": "Postgres"},
        "sources": {"Which database?": "option"},
    }
    assert fresh.answer_mode == "voice"


def test_the_tool_has_nowhere_to_put_a_label(con: sqlite3.Connection) -> None:
    """The structural guarantee, asserted on the schema the model actually reads."""
    tool = next(t for t in ansmod.TOOLS if t.name == "answer_question")
    props = tool.parameters["properties"]
    assert props["option"]["type"] == "INTEGER"
    assert props["options"]["items"]["type"] == "INTEGER"
    # own_words is the ONE string, and it is for the case where no option fits.
    assert set(props) == {"option", "options", "own_words"}
    assert "verbatim" in props["own_words"]["description"].lower()


def test_an_option_that_does_not_exist_is_refused_in_words(con: sqlite3.Connection) -> None:
    req = ask(con)
    with pytest.raises(ansmod.ToolError) as exc:
        ansmod.answer_question(ctx_for(con, req), option=9)
    assert "9" in str(exc.value)
    assert rq.get_request(con, req.id).state == "pending"


def test_several_numbers_answer_a_multi_select(con: sqlite3.Connection) -> None:
    req = ask(con, MULTI)
    ansmod.answer_question(ctx_for(con, req), options=[1, 3])
    assert rq.get_request(con, req.id).answer["answers"] == {
        "Which features?": ["Due dates", "Priorities"]
    }


def test_one_number_on_a_multi_select_still_answers_it(con: sqlite3.Connection) -> None:
    req = ask(con, MULTI)
    ansmod.answer_question(ctx_for(con, req), option=2)
    assert rq.get_request(con, req.id).answer["answers"] == {"Which features?": ["Tags"]}


# ───────────────────────────── none of these ─────────────────────────────


def test_own_words_are_stored_verbatim_and_never_as_a_label(con: sqlite3.Connection) -> None:
    req = ask(con)
    ansmod.answer_question(ctx_for(con, req), own_words="put it in DuckDB")
    stored = rq.get_request(con, req.id).answer
    assert stored["answers"] == {"Which database?": "put it in DuckDB"}
    assert stored["sources"] == {"Which database?": "free_text"}
    assert "Other" not in str(stored)


def test_own_words_that_are_actually_an_option_are_refused(con: sqlite3.Connection) -> None:
    """Otherwise a label reaches the answer by a path that skips the index lookup."""
    req = ask(con)
    with pytest.raises(ansmod.ToolError):
        ansmod.answer_question(ctx_for(con, req), own_words="SQLite")


def test_numbers_and_words_together_are_refused(con: sqlite3.Connection) -> None:
    req = ask(con)
    with pytest.raises(ansmod.ToolError, match="not both"):
        ansmod.answer_question(ctx_for(con, req), option=1, own_words="something else")


def test_neither_numbers_nor_words_asks_again(con: sqlite3.Connection) -> None:
    req = ask(con)
    with pytest.raises(ansmod.ToolError, match="Which one"):
        ansmod.answer_question(ctx_for(con, req))


# ───────────────────────────── which question ─────────────────────────────


def test_it_will_not_guess_which_question_it_is_answering(con: sqlite3.Connection) -> None:
    """Two open questions and nothing read here: refusing is the only safe answer."""
    ask(con)
    ask(con, MULTI)
    with pytest.raises(ansmod.NoQuestionHere) as exc:
        ansmod.answer_question(ctx_for(con), option=1)
    assert "2 waiting" in str(exc.value)
    assert all(r.state == "pending" for r in rq.open_requests(con))


def test_with_nothing_waiting_it_says_so(con: sqlite3.Connection) -> None:
    with pytest.raises(ansmod.NoQuestionHere, match="Nothing is waiting"):
        ansmod.answer_question(ctx_for(con), option=1)


def test_a_question_answered_elsewhere_does_not_carry_a_chat_id_into_the_sentence(
    con: sqlite3.Connection,
) -> None:
    req = ask(con)
    rq.answer_request(
        con,
        req.id,
        {"answers": {"Which database?": "SQLite"}},
        answered_by="telegram:284417331",
        answer_mode="button",
    )
    with pytest.raises(ansmod.AlreadySettled) as exc:
        ansmod.answer_question(ctx_for(con, req), option=2)
    assert "on Telegram" in str(exc.value)
    assert "284417331" not in str(exc.value)


def test_losing_the_race_is_a_sentence_not_an_overwrite(con: sqlite3.Connection) -> None:
    """First answer wins. The loser must SAY so — a live button invites a second try."""
    req = ask(con)
    other = ctx_for(con, req)
    rq.answer_request(
        con, req.id, {"answers": {"Which database?": "SQLite"}}, "telegram:1", "button"
    )
    with pytest.raises(ansmod.AlreadySettled):
        ansmod.answer_question(other, option=2)
    assert rq.get_request(con, req.id).answer["answers"] == {"Which database?": "SQLite"}


# ───────────────────────────── rereading ─────────────────────────────


def test_reread_returns_the_exact_numbered_lines(con: sqlite3.Connection) -> None:
    req = ask(con)
    text = ansmod.reread_options(ctx_for(con, req))
    assert "1. SQLite" in text and "2. Postgres" in text
    assert text.splitlines()[0] == req.presentation["intro"]


def test_reread_adds_nothing_of_its_own(con: sqlite3.Connection) -> None:
    req = ask(con)
    text = ansmod.reread_options(ctx_for(con, req))
    for line in text.splitlines():
        assert (
            line in req.presentation["intro"]
            or line.split(". ", 1)[-1] in {i["label"] for i in req.presentation["items"]}
            or line == req.presentation.get("free_text_prompt")
        )


# ───────────────────────────── the surface ─────────────────────────────


def test_every_channel_that_can_be_read_to_can_answer() -> None:
    reg = registry()
    for channel in ("desk", "telegram", "phone", "cli"):
        assert "answer_question" in reg.names(channel), channel
        assert "reread_options" in reg.names(channel), channel


def test_the_desk_profile_now_offers_the_tools_it_names() -> None:
    """DESK.tools was a list of four names that did not exist. Two of them now do."""
    from jarvis.live.profiles import DESK
    from jarvis.voice.tools import LiveTools

    lt = LiveTools(registry=registry(), open_db=lambda: None, channel="desk")
    offered = [d["name"] for d in lt.declarations(DESK)]
    assert "answer_question" in offered
    assert "reread_options" in offered
    assert set(lt.unresolved(DESK)) == {"explain_option", "job_control"}


def test_a_refusal_reaches_the_user_as_a_sentence(con: sqlite3.Connection) -> None:
    """Through the registry, which is where the spine's exception types get lost."""
    req = ask(con)
    said = registry().dispatch("answer_question", {"option": 9}, ctx_for(con, req))
    assert "Sorry" not in said, "a spine LookupError must not surface as a generic failure"
    assert "9" in said
