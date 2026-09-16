"""The routing table: which update goes where, and who is allowed to send one.

The test that earns its place here is the forwarded button. An inline keyboard
can be forwarded to anyone, and a forwarded button tapped by a stranger arrives
as a perfectly well-formed callback carrying a real request id. Authorising only
the message path would leave every permission prompt this system has ever sent
one forward away from being answered by somebody else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import requests as rq
from jarvis.cc import gate
from jarvis.db import connect, migrate
from jarvis.ids import now
from jarvis.telegram import identity, render
from jarvis.telegram.__main__ import dispatch
from jarvis.telegram.channel import TelegramChannel
from jarvis.telegram.transport import FakeTransport

JOB = "job_dispatchtest"
CHAT = 4242
STRANGER = 66613

QUESTION: dict[str, Any] = {
    "questions": [
        {
            "question": "Which database?",
            "options": [{"label": "SQLite"}, {"label": "Postgres"}],
        }
    ]
}


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    c.execute(
        """INSERT INTO jobs (id, kind, title, state, created_at, updated_at, created_by)
           VALUES (?, 'claude_code', 'the todo app build', 'running', ?, ?, 'test')""",
        (JOB, now(), now()),
    )
    code = identity.offer_code(c, by="operator")
    identity.redeem(c, CHAT, code)
    yield c
    c.close()


@pytest.fixture
def channel() -> TelegramChannel:
    return TelegramChannel(chat_id=CHAT)


def _ask(con: sqlite3.Connection) -> rq.Request:
    return gate.ensure_request(
        con,
        job_id=JOB,
        actor="runner",
        tool_name="AskUserQuestion",
        tool_use_id="toolu_dispatch",
        input_data=QUESTION,
    )


def _text(chat_id: int, text: str, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id}, "text": text},
    }


def test_a_stranger_gets_no_reply_at_all(con: sqlite3.Connection, channel: TelegramChannel) -> None:
    """Replying 'you are not authorised' confirms the bot is live and attended."""
    t = FakeTransport()
    dispatch(con, t, _text(STRANGER, "/status"), channel=channel)
    assert t.calls == []
    dropped = con.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='telegram.dropped'"
    ).fetchone()["n"]
    assert dropped == 1


def test_a_forwarded_button_tapped_by_a_stranger_answers_nothing(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con)
    message_id = channel.present(con, FakeTransport(), req)
    t = FakeTransport()
    dispatch(
        con,
        t,
        {
            "update_id": 9,
            "callback_query": {
                "id": "cbq",
                "from": {"id": STRANGER},
                "message": {"message_id": message_id, "chat": {"id": STRANGER}},
                "data": render.encode_callback(req.id, "pick", 1),
            },
        },
        channel=channel,
    )
    assert t.calls == []
    still = rq.get_request(con, req.id)
    assert still is not None and still.state == "pending"


def test_the_bound_chat_can_answer(con: sqlite3.Connection, channel: TelegramChannel) -> None:
    req = _ask(con)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    dispatch(
        con,
        t,
        {
            "update_id": 10,
            "callback_query": {
                "id": "cbq",
                "from": {"id": CHAT},
                "message": {"message_id": message_id, "chat": {"id": CHAT}},
                "data": render.encode_callback(req.id, "pick", 1),
            },
        },
        channel=channel,
    )
    settled = rq.get_request(con, req.id)
    assert settled is not None and settled.state == "answered"


def test_a_command_is_answered_in_the_chat(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    t = FakeTransport()
    dispatch(con, t, _text(CHAT, "/status"), channel=channel)
    sent = t.last("sendMessage")
    assert sent is not None and "the todo app build" in sent.params["text"]


def test_a_reply_to_a_question_is_tried_as_an_answer_before_being_refused(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    req = _ask(con)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    dispatch(
        con,
        t,
        {
            "update_id": 11,
            "message": {
                "message_id": message_id + 1,
                "chat": {"id": CHAT},
                "text": "DuckDB",
                "reply_to_message": {"message_id": message_id},
            },
        },
        channel=channel,
    )
    settled = rq.get_request(con, req.id)
    assert settled is not None
    assert settled.answer["answers"] == {"Which database?": "DuckDB"}


def test_an_unprompted_sentence_is_told_what_to_do_with_itself(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    t = FakeTransport()
    dispatch(con, t, _text(CHAT, "are you there"), channel=channel)
    sent = t.last("sendMessage")
    assert sent is not None and "/help" in sent.params["text"]


def test_a_voice_note_is_saved_and_acknowledged_without_a_speech_model(
    con: sqlite3.Connection, channel: TelegramChannel, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "jarvis.db"))
    t = FakeTransport(files={"voice/AwACAgQ.oga": b"OggS\x00"})
    dispatch(
        con,
        t,
        {
            "update_id": 12,
            "message": {
                "message_id": 5,
                "chat": {"id": CHAT},
                "voice": {"file_id": "AwACAgQ", "duration": 2, "mime_type": "audio/ogg"},
            },
        },
        channel=channel,
    )
    sent = t.last("sendMessage")
    assert sent is not None and "not transcribed" in sent.params["text"]
    saved = con.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='telegram.voice_received'"
    ).fetchone()["n"]
    assert saved == 1


# ─────────────────── binding, through the bot rather than around it ───────────────────
#
# Every other test in this file binds by calling identity.redeem() directly, which
# is how the flow came to be unreachable in the first place: nothing in the
# product ever called it, and no test noticed because the fixture did it instead.
# These drive the operator's ACTUAL path — --bind prints a code, the code is sent
# to the bot as an ordinary message, and the bot is bound by handling that update.


@pytest.fixture
def unbound(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "unbound.db")
    migrate(c)
    yield c
    c.close()


def _code_message(chat_id: int, code: str, *, chat_type: str = "private") -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": chat_id, "username": "operator"},
            "text": code,
        },
    }


def test_sending_the_printed_code_to_the_bot_actually_binds(unbound: sqlite3.Connection) -> None:
    code = identity.offer_code(unbound, by="operator")
    t = FakeTransport()
    dispatch(unbound, t, _code_message(CHAT, code))
    assert identity.bound_chat(unbound) == CHAT
    assert identity.pending_bind(unbound) is None, "a code is spent by use"
    sent = t.last("sendMessage")
    assert sent is not None and "Bound" in sent.params["text"]


def test_a_bound_chat_works_immediately_without_a_restart(unbound: sqlite3.Connection) -> None:
    """The channel used to be built from the binding as it stood at startup."""
    code = identity.offer_code(unbound, by="operator")
    t = FakeTransport()
    dispatch(unbound, t, _code_message(CHAT, code))
    dispatch(unbound, t, _text(CHAT, "/help", update_id=2))
    assert t.last("sendMessage") is not None
    assert "/status" in t.last("sendMessage").params["text"]


def test_a_stranger_messaging_with_no_offer_outstanding_still_gets_silence(
    unbound: sqlite3.Connection,
) -> None:
    dispatch(unbound, FakeTransport(), _text(STRANGER, "ABCD2345"))
    assert identity.bound_chat(unbound) is None
    dropped = unbound.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='telegram.dropped'"
    ).fetchone()["n"]
    assert dropped == 1


def test_a_group_cannot_be_bound(unbound: sqlite3.Connection) -> None:
    """A bound group would hand every member of it a permission button."""
    code = identity.offer_code(unbound, by="operator")
    t = FakeTransport()
    dispatch(unbound, t, _code_message(-100987, code, chat_type="supergroup"))
    assert identity.bound_chat(unbound) is None
    assert t.calls == []


def test_an_already_bound_bot_cannot_be_rebound_by_whoever_messages_first(
    con: sqlite3.Connection,
) -> None:
    """Moving to a new phone is --unbind then --bind, an explicit act."""
    code = identity.offer_code(con, by="operator")
    t = FakeTransport()
    dispatch(con, t, _code_message(STRANGER, code))
    assert identity.bound_chat(con) == CHAT
    assert t.calls == []


def test_a_wrong_code_inside_the_offer_window_says_so_and_burns_an_attempt(
    unbound: sqlite3.Connection,
) -> None:
    # Code-SHAPED but wrong. ("WRONGONE" would not do: O is not in the alphabet,
    # so it is small talk rather than a guess and spends nothing.)
    guess = "WRQNGXYZ"
    assert all(c in identity.CODE_ALPHABET for c in guess)
    identity.offer_code(unbound, by="operator")
    t = FakeTransport()
    dispatch(unbound, t, _code_message(CHAT, guess))
    assert identity.bound_chat(unbound) is None
    pending = identity.pending_bind(unbound)
    assert pending is not None and pending.attempts_left == identity.DEFAULT_ATTEMPTS - 1
    assert "No:" in t.last("sendMessage").params["text"]


def test_a_group_member_who_is_not_the_bound_chat_cannot_tap_a_button(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """The callback's chat can be the bound one while the TAPPER is not."""
    req = _ask(con)
    message_id = channel.present(con, FakeTransport(), req)
    t = FakeTransport()
    dispatch(
        con,
        t,
        {
            "update_id": 20,
            "callback_query": {
                "id": "cbq",
                "from": {"id": STRANGER},
                "message": {"message_id": message_id, "chat": {"id": CHAT}},
                "data": render.encode_callback(req.id, "pick", 1),
            },
        },
    )
    assert t.calls == []
    still = rq.get_request(con, req.id)
    assert still is not None and still.state == "pending"


def test_small_talk_during_the_offer_window_does_not_burn_an_attempt(
    unbound: sqlite3.Connection,
) -> None:
    """Three strangers saying 'hi' must not destroy the operator's code."""
    code = identity.offer_code(unbound, by="operator")
    t = FakeTransport()
    for i, chatter in enumerate(("hi", "who is this", "hello?")):
        dispatch(unbound, t, _code_message(STRANGER, chatter) | {"update_id": i + 1})
    pending = identity.pending_bind(unbound)
    assert pending is not None
    assert pending.attempts_left == identity.DEFAULT_ATTEMPTS, "no attempt was spent"
    assert t.calls == [], "and none of them learned the bot is live"
    dispatch(unbound, t, _code_message(CHAT, code) | {"update_id": 9})
    assert identity.bound_chat(unbound) == CHAT, "the code still works afterwards"


def test_a_reply_that_reads_like_a_command_answers_the_question_instead(
    con: sqlite3.Connection, channel: TelegramChannel
) -> None:
    """ "back" is a presence override AND a fine answer to "which branch?"."""
    req = _ask(con)
    t = FakeTransport()
    message_id = channel.present(con, t, req)
    dispatch(
        con,
        t,
        {
            "update_id": 30,
            "message": {
                "message_id": message_id + 1,
                "chat": {"id": CHAT},
                "text": "back",
                "reply_to_message": {"message_id": message_id},
            },
        },
    )
    settled = rq.get_request(con, req.id)
    assert settled is not None and settled.state == "answered"
    assert settled.answer["answers"] == {"Which database?": "back"}
    overrides = con.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind LIKE 'presence%'"
    ).fetchone()["n"]
    assert overrides == 0, "the reply was not read as 'I'm back'"
