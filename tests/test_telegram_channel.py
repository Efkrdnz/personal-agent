"""The channel: present a Presentation, post an Answer, and lose gracefully.

The important test in this file is the RACE. A tap on Telegram and a spoken reply
at the desk go through the same ``UPDATE ... WHERE state='pending'``, so exactly
one wins — and the loser must SAY so, because a live button under a settled
question invites a second decision that would be silently discarded.

Every test drives a FakeTransport. There is no token and no network, which is not
a limitation being worked around here: it is what forces the channel to be a
function of rows and a transport rather than of a live chat.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import requests as rq
from jarvis.cc import gate, narrate
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.telegram import render
from jarvis.telegram.channel import AmbiguousApproval, TelegramChannel, build_answer, needs_confirm
from jarvis.telegram.transport import FakeTransport, TelegramError

JOB = "job_telegramtest"
CHAT = 4242

ONE_QUESTION: dict[str, Any] = {
    "questions": [
        {
            "question": "Which database?",
            "header": "database",
            "options": [
                {"label": "SQLite", "description": "one file"},
                {"label": "Postgres", "description": "a server"},
            ],
        }
    ]
}

MULTI_SELECT: dict[str, Any] = {
    "questions": [
        {
            "question": "Which checks should run?",
            "multiSelect": True,
            "options": [
                {"label": "ruff"},
                {"label": "pytest"},
                {"label": "mypy"},
            ],
        }
    ]
}

TWO_QUESTIONS: dict[str, Any] = {
    "questions": [
        {"question": "Which database?", "options": [{"label": "SQLite"}, {"label": "Postgres"}]},
        {"question": "Which host?", "options": [{"label": "local"}, {"label": "fly.io"}]},
    ]
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    con = connect(p)
    migrate(con)
    con.execute(
        """INSERT INTO jobs (id, kind, title, state, created_at, updated_at, created_by)
           VALUES (?, 'claude_code', 'the todo app build', 'running', ?, ?, 'test')""",
        (JOB, now(), now()),
    )
    con.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def desk(db_path: Path) -> Iterator[sqlite3.Connection]:
    """The desk process: a second connection to the same file."""
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def channel() -> TelegramChannel:
    return TelegramChannel(chat_id=CHAT)


def _ask(con: sqlite3.Connection, payload: dict[str, Any], tuid: str = "toolu_1") -> rq.Request:
    return gate.ensure_request(
        con,
        job_id=JOB,
        actor="runner",
        tool_name="AskUserQuestion",
        tool_use_id=tuid,
        input_data=payload,
    )


def _callback(req: rq.Request, verb: str, index: int, message_id: int, markup: Any = None) -> dict:
    return {
        "id": "cbq_1",
        "from": {"id": CHAT},
        "message": {
            "message_id": message_id,
            "chat": {"id": CHAT},
            "reply_markup": markup,
        },
        "data": render.encode_callback(req.id, verb, index),  # type: ignore[arg-type]
    }


# ───────────────────────────── presenting ─────────────────────────────


def test_presenting_sends_the_literal_options_and_a_keyboard(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)

    sent = t.last("sendMessage")
    assert sent is not None
    assert "1. SQLite" in sent.params["text"]
    assert "2. Postgres" in sent.params["text"]
    assert sent.params["chat_id"] == CHAT
    assert "parse_mode" not in sent.params, "a parse mode would stop the labels being literal"
    assert message_id > 0, "the message id is how a reply finds this question again"


def test_the_offered_event_is_the_message_to_question_map(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    from jarvis.telegram.channel import request_for_message

    req = _ask(con, ONE_QUESTION)
    message_id = channel.present(con, FakeTransport(), req)
    assert request_for_message(con, CHAT, message_id) == req.id
    assert request_for_message(con, CHAT, message_id + 1) is None
    assert request_for_message(con, CHAT + 1, message_id) is None


def test_a_due_delivery_is_claimed_before_it_is_presented(
    con: sqlite3.Connection, desk: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    rq.schedule_delivery(con, req.id, "telegram", now())
    t = FakeTransport()
    assert channel.claim_and_present(con, t) == [req.id]

    # A second bot that has not noticed it was replaced must not ask again.
    second = TelegramChannel(chat_id=CHAT, actor="telegram-2")
    assert second.claim_and_present(desk, FakeTransport()) == []
    assert len(t.sent("sendMessage")) == 1


def test_a_delivery_for_an_already_answered_question_is_never_sent(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    rq.schedule_delivery(con, req.id, "telegram", now())
    rq.answer_request(con, req.id, {"answers": {"Which database?": "SQLite"}}, "desk")
    t = FakeTransport()
    assert channel.claim_and_present(con, t) == []
    assert t.sent("sendMessage") == []


def test_a_send_that_fails_marks_the_delivery_rather_than_crashing(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    delivery = rq.schedule_delivery(con, req.id, "telegram", now())
    t = FakeTransport(errors={"sendMessage": [TelegramError("sendMessage", 403, "blocked")]})
    assert channel.claim_and_present(con, t) == []
    row = con.execute("SELECT state, error FROM deliveries WHERE id=?", (delivery.id,)).fetchone()
    assert row["state"] == "failed"
    assert "blocked" in row["error"]


# ───────────────────────────── answering ─────────────────────────────


def test_one_tap_answers_a_single_select_question(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    outcome = channel.on_callback(con, t, _callback(req, "pick", 2, message_id))

    assert outcome.won is True
    settled = rq.get_request(con, req.id)
    assert settled is not None and settled.state == "answered"
    assert settled.answer == {
        "answers": {"Which database?": "Postgres"},
        "sources": {"Which database?": "option"},
    }
    assert settled.answer_mode == "button"
    # And the CLI's own validator would accept it.
    narrate.validate_answers(ONE_QUESTION, settled.answer["answers"])


def test_the_buttons_go_away_once_the_question_is_settled(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    channel.on_callback(con, t, _callback(req, "pick", 1, message_id))

    edit = t.last("editMessageText")
    assert edit is not None
    assert edit.params["reply_markup"] == {"inline_keyboard": []}
    assert "SQLite" in edit.params["text"]


def test_the_desk_and_telegram_race_through_one_compare_and_swap(
    con: sqlite3.Connection, desk: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """The whole point of the second channel, in one test."""
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)

    # The desk hears the answer first, from another process.
    assert rq.answer_request(
        desk, req.id, {"answers": {"Which database?": "SQLite"}}, "desk", "voice"
    )

    outcome = channel.on_callback(con, t, _callback(req, "pick", 2, message_id))
    assert outcome.won is False

    settled = rq.get_request(con, req.id)
    assert settled is not None
    assert settled.answered_by == "desk"
    assert settled.answer == {"answers": {"Which database?": "SQLite"}}

    edit = t.last("editMessageText")
    assert edit is not None
    assert "desk" in edit.params["text"]
    assert edit.params["reply_markup"] == {"inline_keyboard": []}
    toast = t.last("answerCallbackQuery")
    assert toast is not None and "desk" in toast.params["text"]


def test_a_tap_on_a_question_answered_by_the_timeout_says_so(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = gate.ensure_request(
        con,
        job_id=JOB,
        actor="runner",
        tool_name="Bash",
        tool_use_id="toolu_bash",
        input_data={"command": "rm -rf build"},
        on_timeout="deny",
        expires_in_s=0,
    )
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    rq.expire_due(con)

    outcome = channel.on_callback(con, t, _callback(req, "pick", 1, message_id))
    assert outcome.won is False
    edit = t.last("editMessageText")
    assert edit is not None and "timeout" in edit.params["text"]


# ───────────────────────────── multi-select ─────────────────────────────


def test_multi_select_toggles_then_confirms(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, MULTI_SELECT)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    markup = t.last("sendMessage").params["reply_markup"]  # type: ignore[union-attr]

    channel.on_callback(con, t, _callback(req, "toggle", 1, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "toggle", 3, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    assert render.selected_from_markup(markup) == (1, 3)

    # Nothing has been decided yet: a toggle is not an answer.
    pending = rq.get_request(con, req.id)
    assert pending is not None and pending.state == "pending"

    outcome = channel.on_callback(con, t, _callback(req, "confirm", 0, message_id, markup))
    assert outcome.won is True
    settled = rq.get_request(con, req.id)
    assert settled is not None
    assert settled.answer["answers"] == {"Which checks should run?": ["ruff", "mypy"]}


def test_untoggling_removes_the_option_again(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, MULTI_SELECT)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    markup = t.last("sendMessage").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "toggle", 2, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "toggle", 2, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    assert render.selected_from_markup(markup) == ()


def test_confirming_nothing_is_refused_rather_than_answered(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, MULTI_SELECT)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    markup = t.last("sendMessage").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "confirm", 0, message_id, markup))
    pending = rq.get_request(con, req.id)
    assert pending is not None and pending.state == "pending"


def test_a_batch_of_two_questions_cannot_be_answered_by_one_tap(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """Indices are numbered across the batch, so one tap leaves a question unasked."""
    req = _ask(con, TWO_QUESTIONS)
    assert needs_confirm(req) is True
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    markup = t.last("sendMessage").params["reply_markup"]  # type: ignore[union-attr]
    verbs = {
        render.decode_callback(b["callback_data"]).verb
        for row in markup["inline_keyboard"]
        for b in row
    }
    assert "pick" not in verbs

    channel.on_callback(con, t, _callback(req, "toggle", 1, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "toggle", 4, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "confirm", 0, message_id, markup))

    settled = rq.get_request(con, req.id)
    assert settled is not None
    assert settled.answer["answers"] == {"Which database?": "SQLite", "Which host?": "fly.io"}


def test_a_half_answered_batch_leaves_the_question_pending(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, TWO_QUESTIONS)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    markup = t.last("sendMessage").params["reply_markup"]  # type: ignore[union-attr]
    channel.on_callback(con, t, _callback(req, "toggle", 1, message_id, markup))
    markup = t.last("editMessageReplyMarkup").params["reply_markup"]  # type: ignore[union-attr]
    outcome = channel.on_callback(con, t, _callback(req, "confirm", 0, message_id, markup))

    assert outcome.handled is False
    still = rq.get_request(con, req.id)
    assert still is not None and still.state == "pending"
    assert "Which host?" in t.last("sendMessage").params["text"]  # type: ignore[union-attr]


# ───────────────────────────── free text ─────────────────────────────


def test_a_typed_reply_answers_in_the_users_own_words(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    outcome = channel.on_reply(
        con,
        t,
        {
            "message_id": message_id + 5,
            "chat": {"id": CHAT},
            "text": "DuckDB, actually",
            "reply_to_message": {"message_id": message_id},
        },
    )
    assert outcome.won is True
    settled = rq.get_request(con, req.id)
    assert settled is not None
    assert settled.answer["answers"] == {"Which database?": "DuckDB, actually"}
    # 'none of these' is the user's OWN WORDS, never a label and never "Other".
    assert settled.answer["sources"] == {"Which database?": "free_text"}


def test_a_reply_naming_an_offered_option_is_refused_rather_than_guessed(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """Typing 'Postgres' must be picked by NUMBER; a label may never be typed in."""
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    outcome = channel.on_reply(
        con,
        t,
        {
            "message_id": message_id + 1,
            "chat": {"id": CHAT},
            "text": "Postgres",
            "reply_to_message": {"message_id": message_id},
        },
    )
    assert outcome.handled is False
    still = rq.get_request(con, req.id)
    assert still is not None and still.state == "pending"


def test_a_reply_to_something_else_is_left_alone(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    t = FakeTransport()
    outcome = channel.on_reply(
        con,
        t,
        {
            "message_id": 3,
            "chat": {"id": CHAT},
            "text": "hello",
            "reply_to_message": {"message_id": 999},
        },
    )
    assert outcome.handled is False
    assert t.calls == []


# ───────────────────────────── approvals ─────────────────────────────


def test_approving_a_plan_sets_the_boolean_the_driver_reads(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = gate.ensure_request(
        con,
        job_id=JOB,
        actor="runner",
        tool_name="ExitPlanMode",
        tool_use_id="toolu_plan",
        input_data={"plan": "build the thing"},
    )
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    channel.on_callback(con, t, _callback(req, "pick", 1, message_id))
    settled = rq.get_request(con, req.id)
    assert settled is not None and settled.answer["approved"] is True


def test_the_second_option_denies(con: sqlite3.Connection, channel: TelegramChannel) -> None:
    req = gate.ensure_request(
        con,
        job_id=JOB,
        actor="runner",
        tool_name="Bash",
        tool_use_id="toolu_b",
        input_data={"command": "git push --force"},
    )
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    channel.on_callback(con, t, _callback(req, "pick", 2, message_id))
    settled = rq.get_request(con, req.id)
    assert settled is not None and settled.answer["approved"] is False


def test_typing_instead_of_tapping_never_approves_anything(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """Somebody who types is asking for something other than what was offered."""
    req = gate.ensure_request(
        con,
        job_id=JOB,
        actor="runner",
        tool_name="ExitPlanMode",
        tool_use_id="toolu_plan2",
        input_data={"plan": "build the thing"},
    )
    answer = build_answer(req, free_text="use Postgres instead")
    assert answer["approved"] is False
    assert answer["text"] == "use Postgres instead"


def test_a_reordered_option_array_refuses_to_answer_rather_than_inverting_it(
    con: sqlite3.Connection,
) -> None:
    """If gate.py ever puts Deny first, this must break loudly, not approve."""
    pres = rq.make_presentation(
        intro="Allow Bash?",
        options=[{"label": "Deny"}, {"label": "Allow"}],
        question="Allow Bash?",
    )
    req = rq.create_request(
        con,
        kind="tool_permission",
        short_label="bash approval",
        presentation=pres,
        payload={"command": "ls"},
        actor="runner",
        job_id=JOB,
        tool_use_id="toolu_reordered",
        tool_name="Bash",
    )
    with pytest.raises(AmbiguousApproval):
        build_answer(req, picks=(1,))


# ───────────────────────────── stale buttons ─────────────────────────────


def test_a_button_for_a_request_that_no_longer_exists_says_so(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    t = FakeTransport()
    outcome = channel.on_callback(
        con,
        t,
        {
            "id": "cbq",
            "message": {"message_id": 1, "chat": {"id": CHAT}},
            "data": render.encode_callback("req_gone", "pick", 1),
        },
    )
    assert outcome.handled is False
    toast = t.last("answerCallbackQuery")
    assert toast is not None and "no longer" in toast.params["text"]


def test_a_button_from_an_older_grammar_is_not_misread(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    t = FakeTransport()
    outcome = channel.on_callback(
        con,
        t,
        {"id": "cbq", "message": {"message_id": 1, "chat": {"id": CHAT}}, "data": "j0:x:p:1"},
    )
    assert outcome.handled is False


def test_a_cosmetic_edit_failing_never_undoes_a_committed_answer(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """editMessageText 400s on a message older than 48h. The answer still stands."""
    req = _ask(con, ONE_QUESTION)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    t.errors["editMessageText"] = [
        TelegramError("editMessageText", 400, "message to edit not found")
    ]
    outcome = channel.on_callback(con, t, _callback(req, "pick", 1, message_id))
    assert outcome.won is True
    settled = rq.get_request(con, req.id)
    assert settled is not None and settled.state == "answered"
