"""The unified gate, tested where it actually breaks.

Every test here is either a RACE (two real connections to the same file, which
is what two OS processes look like from SQLite's point of view), a RESTART (write
on one connection, close it, assert from another), or a FAILURE PATH. The happy
path is asserted only where it is the contract — an answer coming back verbatim
after a four-hour gap is the whole product.

Two connections are not a simulation of concurrency here. SQLite's locking is
per-connection, not per-process, so two connections racing on one file exercise
exactly the code paths two daemons would.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.ids import dedupe_key, now

# ───────────────────────────── fixtures ─────────────────────────────

JOB = "job_testtesttest"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A throwaway database file. Never the real one at ~/.local/state."""
    p = tmp_path / "jarvis.db"
    con = connect(p)
    migrate(con)
    # requests.job_id is a real FK and foreign_keys is ON, so a job must exist.
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
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A SECOND connection to the same file — the phone, or a resumed driver."""
    c = connect(db_path)
    yield c
    c.close()


QUESTION = "How should todos be stored?"
# The real tool name the driver passes. The dedupe key is hashed over it, so a
# test that used the request KIND here would agree with a create() that made the
# same mistake and prove nothing.
TOOL = "AskUserQuestion"

PRES = rq.make_presentation(
    intro=QUESTION,
    options=[
        {"label": "SQLite", "description": "a local database file"},
        {"label": "JSON file", "description": "a single JSON file"},
        {"label": "Plain text", "description": "one per line"},
    ],
    question=QUESTION,
)

PAYLOAD = {
    "questions": [
        {
            "header": "Storage",
            "question": QUESTION,
            "options": [{"label": "SQLite"}, {"label": "JSON file"}, {"label": "Plain text"}],
            "multiSelect": False,
        }
    ]
}


def mk(con: sqlite3.Connection, **kw: object) -> rq.Request:
    """A plan_question with the boring fields filled in."""
    args: dict[str, object] = {
        "kind": "plan_question",
        "short_label": "storage choice",
        "presentation": PRES,
        "payload": PAYLOAD,
        "actor": "runner:job_testtesttest",
        "job_id": JOB,
        "tool_name": TOOL,
    }
    args.update(kw)
    return rq.create_request(con, **args)  # type: ignore[arg-type]


# ───────────────────── creation: the resume path ─────────────────────


def test_same_tool_use_id_returns_the_existing_row_not_an_error(con: sqlite3.Connection) -> None:
    # S1 measured tool_use_id stable across defer and resume. A resumed driver
    # re-asks; if that raised, every resume would be a crash.
    a = mk(con, tool_use_id="toolu_01G6XWrHkijLpMNjMjnBeabu")
    b = mk(con, tool_use_id="toolu_01G6XWrHkijLpMNjMjnBeabu")
    assert a.id == b.id
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_recreate_after_the_answer_lands_returns_the_answered_row(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # The actual resume shape: create, die, someone answers, a NEW process
    # creates "again" and must get the row that already carries the answer.
    a = mk(con, tool_use_id="toolu_resume")
    rq.answer_request(other, a.id, {"answers": {QUESTION: "JSON file"}}, "telegram", "button")
    again = mk(con, tool_use_id="toolu_resume")
    assert again.id == a.id
    assert again.state == "answered"
    assert again.answer == {"answers": {QUESTION: "JSON file"}}


def test_two_processes_creating_the_same_question_make_one_row(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # Both drivers miss the lookup at the same instant. BEGIN IMMEDIATE means one
    # waits; without it the loser dies on the partial unique index instead of
    # finding the question it was meant to answer.
    a = mk(con, tool_use_id="toolu_race")
    b = mk(other, tool_use_id="toolu_race")
    assert a.id == b.id
    assert other.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_dedupe_key_collision_reuses_the_row_even_with_a_new_tool_use_id(
    con: sqlite3.Connection,
) -> None:
    # UNIQUE(job_id, dedupe_key, attempt) would reject the insert, so returning
    # the existing row is the only outcome that lets the caller reach the answer.
    a = mk(con, tool_use_id="toolu_first")
    b = mk(con, tool_use_id="toolu_second")
    assert a.id == b.id
    assert b.tool_use_id == "toolu_first"


def test_a_genuine_reask_is_a_new_row_at_attempt_two(con: sqlite3.Connection) -> None:
    # Claude legitimately asking the same question twice is NOT a re-fire. The
    # attempt counter is the only thing that tells them apart.
    first = mk(con)
    key = dedupe_key(JOB, TOOL, PAYLOAD)
    n = rq.next_attempt(con, JOB, first.dedupe_key)
    assert first.dedupe_key == key
    assert n == 2
    second = mk(con, attempt=n)
    assert second.id != first.id
    assert second.attempt == 2


def test_request_for_a_job_that_does_not_exist_is_refused(con: sqlite3.Connection) -> None:
    # foreign_keys is ON per connection; a typo'd job id must not create an
    # orphan question nobody will ever route.
    with pytest.raises(sqlite3.IntegrityError):
        mk(con, job_id="job_nope")


def test_payload_is_stored_byte_for_byte_not_canonicalised(con: sqlite3.Connection) -> None:
    # The driver returns {**payload, "answers": ...} and the CLI's validator
    # rejects a changed shown field. Sorting keys or escaping Turkish here would
    # be invisible until a real build was silently rejected.
    payload = {"zeta": 1, "alpha": {"nested": "üç"}, "questions": [], "ratio": 0.5}
    r = mk(con, payload=payload, tool_use_id="toolu_bytes")
    raw = con.execute("SELECT payload FROM requests WHERE id=?", (r.id,)).fetchone()[0]
    assert raw == '{"zeta":1,"alpha":{"nested":"üç"},"questions":[],"ratio":0.5}'
    assert list(r.payload) == ["zeta", "alpha", "questions", "ratio"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "gossip"), ("urgency", "urgent"), ("on_timeout", "hang"), ("attempt", 0)],
)
def test_bad_vocabulary_is_refused_at_the_door(
    con: sqlite3.Connection, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        mk(con, **{field: value})


def test_actor_is_required_even_though_it_is_not_stored(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        mk(con, actor="")


# ───────────────────── the answer race ─────────────────────


def test_two_channels_racing_produce_exactly_one_winner(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # The desk and the phone, both sure they have the user's attention.
    r = mk(con, tool_use_id="toolu_cas")
    desk = rq.answer_request(con, r.id, {"answers": {QUESTION: "SQLite"}}, "desk", "voice")
    phone = rq.answer_request(other, r.id, {"answers": {QUESTION: "JSON file"}}, "phone", "dtmf")
    assert [desk, phone] == [True, False]

    stored = rq.get_request(other, r.id)
    assert stored is not None
    assert stored.answer == {"answers": {QUESTION: "SQLite"}}
    assert stored.answered_by == "desk"


def test_a_genuine_simultaneous_race_still_has_one_winner(db_path: Path) -> None:
    """Both answers issued at the same instant, on two connections, in two threads.

    The sequential test above proves the predicate; this one proves the LOCKING —
    that BEGIN IMMEDIATE plus busy_timeout makes the loser wait and then see
    'answered' rather than corrupting the row or raising SQLITE_BUSY.
    """
    setup = connect(db_path)
    r = mk(setup, tool_use_id="toolu_thread_race")
    setup.close()

    gate = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def answerer(channel: str, label: str) -> None:
        c = connect(db_path)
        try:
            gate.wait(timeout=5)
            won = rq.answer_request(c, r.id, {"answers": {QUESTION: label}}, channel, "voice")
            with lock:
                results.append(won)
        finally:
            c.close()

    threads = [
        threading.Thread(target=answerer, args=("desk", "SQLite")),
        threading.Thread(target=answerer, args=("phone", "JSON file")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False, True]

    check = connect(db_path)
    try:
        stored = rq.get_request(check, r.id)
        assert stored is not None and stored.state == "answered"
        # Whoever won, the answer and the attribution must belong to the SAME
        # channel. A torn row here would be a decision nobody made.
        winner = {"desk": "SQLite", "phone": "JSON file"}[stored.answered_by or ""]
        assert stored.answer == {"answers": {QUESTION: winner}}
    finally:
        check.close()


def test_the_loser_is_told_it_lost_rather_than_failing_silently(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # A False here is what makes Jarvis say "never mind, that was answered on the
    # phone". A raise, or a silent True, would strand the user mid-sentence.
    r = mk(con)
    rq.answer_request(con, r.id, {"approved": True}, "desk", "voice")
    assert rq.answer_request(other, r.id, {"approved": False}, "telegram", "button") is False


def test_answering_an_unknown_id_raises_and_is_not_confused_with_losing(
    con: sqlite3.Connection,
) -> None:
    # Both produce "no row updated"; only one of them is a caller bug.
    with pytest.raises(rq.UnknownRequest):
        rq.answer_request(con, "req_doesnotexist", {"approved": True}, "desk", "voice")


@pytest.mark.parametrize(
    "answer",
    [
        {},  # "answered" with nothing: the failure that looks like success
        {"response": "SQLite"},  # S1: silently discards the per-question answers
        {"answers": {}, "extra": 1},  # unknown key: not the wire format
    ],
)
def test_malformed_answers_are_refused(con: sqlite3.Connection, answer: dict) -> None:
    r = mk(con)
    with pytest.raises(ValueError):
        rq.answer_request(con, r.id, answer, "desk", "voice")  # type: ignore[arg-type]
    assert rq.get_request(con, r.id).state == "pending"  # type: ignore[union-attr]


def test_anonymous_answers_are_refused(con: sqlite3.Connection) -> None:
    r = mk(con)
    with pytest.raises(ValueError):
        rq.answer_request(con, r.id, {"approved": True}, "", "voice")


def test_a_cancelled_request_can_no_longer_be_answered(con: sqlite3.Connection) -> None:
    r = mk(con)
    assert rq.cancel_request(con, r.id) is True
    assert rq.cancel_request(con, r.id) is False
    assert rq.answer_request(con, r.id, {"approved": True}, "desk", "voice") is False


def test_superseded_is_a_terminal_that_blocks_answers_too(con: sqlite3.Connection) -> None:
    r = mk(con)
    assert rq.supersede_request(con, r.id) is True
    assert rq.answer_request(con, r.id, {"approved": True}, "desk", "voice") is False


# ───────────────────── restart survival ─────────────────────


def test_the_four_hour_phone_answer(db_path: Path) -> None:
    """Create here, die, answer over there, resume somewhere else entirely."""
    driver = connect(db_path)
    r = mk(driver, tool_use_id="toolu_fourhours")
    driver.close()  # the runner is SIGKILLed; nothing survives in memory

    phone = connect(db_path)
    assert rq.answer_request(
        phone, r.id, {"answers": {QUESTION: "Postgres, actually"}}, "phone", "voice"
    )
    phone.close()  # autocommit, or S1's silent rollback loses the answer

    resumed = connect(db_path)
    try:
        # The microsecond path: the question re-fires and the answer is already there.
        assert rq.find_answer(resumed, "toolu_fourhours") == {
            "answers": {QUESTION: "Postgres, actually"}
        }
        assert rq.consume(resumed, r.id) == {"answers": {QUESTION: "Postgres, actually"}}
    finally:
        resumed.close()


def test_find_answer_is_none_while_pending_and_for_an_unknown_tool_use_id(
    con: sqlite3.Connection,
) -> None:
    # A None here means "block / defer". A stale or guessed answer would let the
    # build proceed on a decision no human made.
    mk(con, tool_use_id="toolu_open")
    assert rq.find_answer(con, "toolu_open") is None
    assert rq.find_answer(con, "toolu_never_existed") is None


def test_find_answer_still_answers_after_consumption(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # Consumption is not expiry: a driver that resumes twice must not be told
    # the question is unanswered.
    r = mk(con, tool_use_id="toolu_twice")
    rq.answer_request(other, r.id, {"answers": {QUESTION: "SQLite"}}, "desk", "voice")
    rq.consume(con, r.id)
    assert rq.find_answer(other, "toolu_twice") == {"answers": {QUESTION: "SQLite"}}


# ───────────────────── consumption ─────────────────────


def test_consume_is_idempotent_across_connections(db_path: Path) -> None:
    a = connect(db_path)
    r = mk(a, tool_use_id="toolu_consume")
    rq.answer_request(a, r.id, {"answers": {QUESTION: "SQLite"}}, "desk", "voice")
    first = rq.consume(a, r.id)
    stamp = rq.get_request(a, r.id).consumed_at  # type: ignore[union-attr]
    a.close()

    b = connect(db_path)
    try:
        assert rq.consume(b, r.id) == first
        # The first consumption is the one that happened; replays must not
        # rewrite history, or "when did Jarvis tell me" answers wrongly.
        assert rq.get_request(b, r.id).consumed_at == stamp  # type: ignore[union-attr]
    finally:
        b.close()


def test_consuming_a_pending_request_returns_none_not_a_guess(con: sqlite3.Connection) -> None:
    r = mk(con)
    assert rq.consume(con, r.id) is None
    assert rq.get_request(con, r.id).state == "pending"  # type: ignore[union-attr]


def test_consuming_an_unknown_request_raises(con: sqlite3.Connection) -> None:
    with pytest.raises(rq.UnknownRequest):
        rq.consume(con, "req_gone")


# ───────────────────── find_open_for_tool ─────────────────────


def test_lookup_prefers_tool_use_id_then_dedupe_key(con: sqlite3.Connection) -> None:
    r = mk(con, tool_use_id="toolu_lookup")
    assert rq.find_open_for_tool(con, JOB, "toolu_lookup", "x", {}).id == r.id  # type: ignore[union-attr]
    # No tool_use_id: fall through to the content hash of the very same call.
    by_key = rq.find_open_for_tool(con, JOB, None, TOOL, PAYLOAD)
    assert by_key is not None and by_key.id == r.id


def test_a_consumed_row_is_not_reused_for_a_fresh_ask(con: sqlite3.Connection) -> None:
    # Asked, answered AND served. Claude asking again is a second question.
    r = mk(con)
    rq.answer_request(con, r.id, {"approved": True}, "desk", "voice")
    rq.consume(con, r.id)
    assert rq.find_open_for_tool(con, JOB, None, TOOL, PAYLOAD) is None


def test_lookup_misses_when_the_tool_input_differs_by_one_byte(con: sqlite3.Connection) -> None:
    mk(con)
    other_input = {"questions": [{"question": "something else"}]}
    assert rq.find_open_for_tool(con, JOB, None, TOOL, other_input) is None


# ───────────────────── expiry: every branch of the typed enum ─────────────────────


def past(con: sqlite3.Connection, **kw: object) -> rq.Request:
    """A request that was already overdue when it was created."""
    return mk(con, expires_in_s=-1, **kw)


def test_defer_keeps_the_question_alive_and_stops_it_firing_again(
    con: sqlite3.Connection,
) -> None:
    r = past(con, on_timeout="defer", tool_use_id="toolu_defer")
    [out] = rq.expire_due(con)
    assert (out.outcome, out.state, out.request_id) == ("deferred", "pending", r.id)

    after = rq.get_request(con, r.id)
    assert after is not None
    assert after.state == "pending"
    assert after.expires_at is None  # or every later sweep re-fires it forever
    assert rq.expire_due(con) == []
    # It is still answerable four hours later — that is the entire point.
    assert rq.answer_request(con, r.id, {"answers": {QUESTION: "SQLite"}}, "phone", "voice")


def test_deny_writes_a_real_denial_the_driver_can_read(con: sqlite3.Connection) -> None:
    # Not a state nobody polls: the denial travels on the answer column, so the
    # blocked runner returns PermissionResultDeny instead of assuming.
    r = past(con, on_timeout="deny")
    [out] = rq.expire_due(con)
    assert (out.outcome, out.state) == ("denied", "answered")

    assert rq.consume(con, r.id) == {"approved": False, "text": rq.TIMEOUT_DENY_TEXT}
    after = rq.get_request(con, r.id)
    assert after is not None and after.answered_by == "timeout"
    assert after.answer_mode == "timeout"


def test_default_applies_the_answer_that_was_written_down_in_advance(
    con: sqlite3.Connection,
) -> None:
    pres = rq.make_presentation(
        intro="Good moment for your briefing?",
        options=["Now", "Five minutes", "Skip today"],
        question="Good moment for your briefing?",
        default_answer={"answers": {"Good moment for your briefing?": "Skip today"}},
    )
    r = past(con, on_timeout="default", presentation=pres, kind="briefing_gate")
    [out] = rq.expire_due(con)
    assert out.outcome == "defaulted"
    assert rq.consume(con, r.id) == {"answers": {"Good moment for your briefing?": "Skip today"}}


def test_default_with_no_default_written_down_expires_and_says_why(
    con: sqlite3.Connection,
) -> None:
    # Falling back to option one here is exactly the guess this module exists to
    # refuse. Expiring loudly is the honest outcome.
    r = past(con, on_timeout="default")
    [out] = rq.expire_due(con)
    assert (out.outcome, out.state) == ("expired", "expired")
    assert "default_answer" in out.reason
    after = rq.get_request(con, r.id)
    assert after is not None and after.answer is None
    assert rq.answer_request(con, r.id, {"approved": True}, "desk", "voice") is False


def test_escalate_releases_the_channel_and_hands_the_clock_to_the_router(
    con: sqlite3.Connection,
) -> None:
    r = past(con, on_timeout="escalate")
    d = rq.schedule_delivery(con, r.id, "desk", now())
    rq.claim_delivery(con, d.id, "voice")

    [out] = rq.expire_due(con)
    assert (out.outcome, out.state) == ("escalated", "pending")

    assert _delivery_state(con, d.id) == "released"
    assert rq.due_deliveries(con) == []  # released is the router's to re-ladder
    assert rq.get_request(con, r.id).expires_at is None  # type: ignore[union-attr]


def test_a_human_answering_first_beats_the_sweep(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # The answer arrives on the phone the same second the deadline passes. The
    # CAS predicate, not ordering luck, is what protects it.
    r = past(con, on_timeout="deny")
    assert rq.answer_request(other, r.id, {"answers": {QUESTION: "SQLite"}}, "phone", "voice")
    assert rq.expire_due(con) == []
    after = rq.get_request(con, r.id)
    assert after is not None and after.answer == {"answers": {QUESTION: "SQLite"}}


def test_two_processes_sweeping_at_once_expire_each_row_once(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # dispatch restarting while the old one is still alive. Both see the row as
    # due; only one may act on it, or the user hears the same denial twice.
    r = past(con, on_timeout="deny")
    first = rq.expire_due(con)
    second = rq.expire_due(other)
    assert [e.request_id for e in first] == [r.id]
    assert second == []


def test_a_request_with_no_deadline_never_expires(con: sqlite3.Connection) -> None:
    mk(con, on_timeout="deny")  # expires_in_s omitted
    assert rq.expire_due(con) == []


def test_expiry_outcomes_survive_a_restart(db_path: Path) -> None:
    a = connect(db_path)
    r = past(a, on_timeout="deny")
    rq.expire_due(a)
    a.close()
    b = connect(db_path)
    try:
        assert rq.consume(b, r.id) == {"approved": False, "text": rq.TIMEOUT_DENY_TEXT}
    finally:
        b.close()


# ───────────────────── deliveries ─────────────────────


def _delivery_state(con: sqlite3.Connection, delivery_id: str) -> str:
    return con.execute("SELECT state FROM deliveries WHERE id=?", (delivery_id,)).fetchone()[0]


def test_rescheduling_the_same_rung_is_idempotent(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # The router re-materialises its ladder on every restart.
    r = mk(con)
    a = rq.schedule_delivery(con, r.id, "telegram", now())
    b = rq.schedule_delivery(other, r.id, "telegram", now())
    assert a.id == b.id


def test_two_presenters_race_for_one_delivery_and_one_loses(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    r = mk(con)
    d = rq.schedule_delivery(con, r.id, "desk", now())
    assert rq.claim_delivery(con, d.id, "voice-old", lease_s=60) is True
    assert rq.claim_delivery(other, d.id, "voice-new", lease_s=60) is False


def test_a_dead_presenters_claim_is_reclaimable_after_its_lease(db_path: Path) -> None:
    a = connect(db_path)
    r = mk(a)
    d = rq.schedule_delivery(a, r.id, "desk", now())
    assert rq.claim_delivery(a, d.id, "voice-old", lease_s=-1)  # already expired
    a.close()  # the presenter dies holding the claim

    b = connect(db_path)
    try:
        assert [x.id for x in rq.due_deliveries(b)] == [d.id]
        assert rq.claim_delivery(b, d.id, "voice-new", lease_s=60) is True
        # And the dead process, were it somehow still alive, cannot claim to
        # have read out a question it never got to.
        assert rq.mark_presented(b, d.id, "voice-old") is False
        assert rq.mark_presented(b, d.id, "voice-new") is True
    finally:
        b.close()


def test_answering_settles_every_open_delivery_in_the_same_transaction(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # Otherwise there is a window where the question is decided and Telegram
    # still believes it should ask it.
    r = mk(con)
    desk = rq.schedule_delivery(con, r.id, "desk", now())
    tg = rq.schedule_delivery(con, r.id, "telegram", now())
    rq.claim_delivery(con, desk.id, "voice")
    rq.mark_presented(con, desk.id, "voice")

    rq.answer_request(other, r.id, {"approved": True}, "telegram", "button")
    assert _delivery_state(other, desk.id) == "answered"
    assert _delivery_state(other, tg.id) == "answered"
    assert rq.due_deliveries(other) == []


def test_cancelling_skips_the_deliveries_it_had_already_scheduled(
    con: sqlite3.Connection,
) -> None:
    r = mk(con)
    d = rq.schedule_delivery(con, r.id, "telegram", now())
    rq.cancel_request(con, r.id)
    assert _delivery_state(con, d.id) == "skipped"


def test_a_delivery_that_is_not_due_yet_is_not_offered(con: sqlite3.Connection) -> None:
    r = mk(con)
    rq.schedule_delivery(con, r.id, "phone", "2099-01-01T00:00:00.000Z")
    assert rq.due_deliveries(con) == []


# ───────────────────── presentation: the label guarantee ─────────────────────


def test_numbering_comes_from_payload_order_not_from_the_labels(
    con: sqlite3.Connection,
) -> None:
    a = rq.make_presentation(intro="q", options=["SQLite", "JSON file"])
    b = rq.make_presentation(intro="q", options=["JSON file", "SQLite"])
    assert [i["index"] for i in a["items"]] == [1, 2]
    assert rq.label_for_index(a, 1) == "SQLite"
    assert rq.label_for_index(b, 1) == "JSON file"


def test_labels_are_copied_verbatim_and_never_tidied(con: sqlite3.Connection) -> None:
    # Trimming, casing or normalising a label would break the CLI's exact-match
    # on the options array, and would do it silently.
    weird = ["  SQLite  ", "üç şey", "JSON file\n"]
    pres = rq.make_presentation(intro="q", options=weird)
    assert rq.labels_for_indices(pres, [1, 2, 3]) == weird


def test_indices_map_back_in_the_order_they_were_spoken(con: sqlite3.Connection) -> None:
    assert rq.labels_for_indices(PRES, [3, 1]) == ["Plain text", "SQLite"]


@pytest.mark.parametrize("bad", [0, -1, 4, 99])
def test_an_index_out_of_range_raises_and_never_falls_back_to_option_one(bad: int) -> None:
    with pytest.raises(rq.OptionIndexError):
        rq.label_for_index(PRES, bad)


def test_a_non_integer_index_raises_rather_than_being_coerced() -> None:
    # "1" from a JSON round-trip would index fine in some languages and select
    # the wrong option in this one.
    for bad in ("1", 1.0, True, None):
        with pytest.raises(rq.OptionIndexError):
            rq.label_for_index(PRES, bad)  # type: ignore[arg-type]


def test_a_presentation_whose_items_are_misnumbered_is_refused(con: sqlite3.Connection) -> None:
    # Hand-built payloads are where off-by-one numbering gets in.
    broken = dict(PRES)
    broken["items"] = [{"index": 0, "label": "SQLite", "description": ""}]
    with pytest.raises(ValueError):
        mk(con, presentation=broken)


def test_a_dtmf_map_can_only_point_at_options_that_exist() -> None:
    with pytest.raises(rq.OptionIndexError):
        rq.make_presentation(intro="q", options=["Now", "Later"], dtmf_map={"1": 1, "9": 7})


def test_an_unmapped_keypress_is_an_error_not_a_default_option() -> None:
    pres = rq.make_presentation(intro="q", options=["Now", "Later"], dtmf_map={"1": 1, "2": 2})
    assert rq.index_for_dtmf(pres, "2") == 2
    with pytest.raises(rq.OptionIndexError):
        rq.index_for_dtmf(pres, "7")


def test_a_default_answer_baked_into_a_presentation_is_validated_early(
    con: sqlite3.Connection,
) -> None:
    # Better to reject it when the question is written than at 3am on timeout.
    with pytest.raises(ValueError):
        rq.make_presentation(intro="q", options=["Now"], default_answer={"response": "Now"})


def test_open_requests_lists_only_the_pending_ones(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    a = mk(con, tool_use_id="toolu_open_a")
    b = mk(con, tool_use_id="toolu_open_b", payload={"questions": ["b"]})
    rq.answer_request(other, a.id, {"approved": True}, "desk", "voice")
    assert [r.id for r in rq.open_requests(con, JOB)] == [b.id]


# ───────────────────── no transaction is ever left open ─────────────────────


def test_early_returns_inside_a_transaction_still_commit_and_release(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Every short-circuit in this module returns from INSIDE ``with tx(con)``.

    A missed COMMIT there would hold the write lock for the life of the process
    and every other daemon would block on it until busy_timeout — which reads as
    "Jarvis froze", not as "someone forgot a commit".
    """
    r = mk(con, tool_use_id="toolu_txleak")
    assert con.in_transaction is False

    mk(con, tool_use_id="toolu_txleak")  # returns the existing row, early
    assert con.in_transaction is False

    rq.answer_request(con, r.id, {"approved": True}, "desk", "voice")
    assert con.in_transaction is False

    assert rq.answer_request(con, r.id, {"approved": False}, "phone", "dtmf") is False
    assert con.in_transaction is False  # the loser's early return

    assert rq.cancel_request(con, r.id) is False
    assert con.in_transaction is False

    with pytest.raises(rq.UnknownRequest):
        rq.answer_request(con, "req_nope", {"approved": True}, "desk", "voice")
    assert con.in_transaction is False  # the raising path rolled back and released

    rq.schedule_delivery(con, r.id, "desk", now())
    rq.schedule_delivery(con, r.id, "desk", now())  # idempotent, early
    assert con.in_transaction is False

    # And the proof that the lock really is free: another connection can write.
    b = mk(other, tool_use_id="toolu_txleak_other", payload={"questions": ["b"]})
    assert rq.answer_request(other, b.id, {"approved": True}, "telegram", "button")


# ───────────────────── review: the seams the first pass left open ─────────────────────


def test_the_drivers_own_sequence_finds_its_row_the_second_time(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Exactly the call order jarvis.cc.driver makes, with no tool_use_id.

    The driver looks up by (job, tool_name, tool_input), misses, creates, and
    looks up again after a restart. If create() hashed its key over anything but
    the tool name the second lookup misses too, the question is asked a second
    time, and the only symptom is a human being asked twice — hours later, on
    another channel, with the first answer already in the database.
    """
    assert rq.find_open_for_tool(con, JOB, None, TOOL, PAYLOAD) is None
    made = rq.create_request(
        con,
        kind="plan_question",
        short_label="storage choice",
        presentation=PRES,
        payload=PAYLOAD,
        actor="runner:job_testtesttest",
        job_id=JOB,
        tool_name=TOOL,
    )
    found = rq.find_open_for_tool(other, JOB, None, TOOL, PAYLOAD)
    assert found is not None and found.id == made.id
    assert made.dedupe_key == dedupe_key(JOB, TOOL, PAYLOAD)


def test_a_rung_laddered_after_the_answer_lands_is_never_presentable(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The router's ladder and a human's answer race on two connections.

    answer_request can only settle the deliveries that exist when it commits, so
    a rung materialised a millisecond later would sit in 'scheduled' forever and
    Telegram would ask a question that already has an answer.
    """
    r = mk(con)
    assert rq.answer_request(con, r.id, {"answers": {QUESTION: "SQLite"}}, "desk", "voice")

    late = rq.schedule_delivery(other, r.id, "telegram", now())
    assert late.state == "skipped"
    assert rq.due_deliveries(other) == []
    assert rq.claim_delivery(other, late.id, "telegram-bot") is False


def test_a_rung_laddered_after_a_cancel_is_skipped_too(con: sqlite3.Connection) -> None:
    r = mk(con)
    rq.cancel_request(con, r.id)
    assert rq.schedule_delivery(con, r.id, "phone", now()).state == "skipped"


def test_a_rung_for_a_request_that_does_not_exist_still_fails_loudly(
    con: sqlite3.Connection,
) -> None:
    # Filing an unroutable rung as merely 'skipped' would hide a caller bug.
    with pytest.raises(sqlite3.IntegrityError):
        rq.schedule_delivery(con, "req_nope", "desk", now())


@pytest.mark.parametrize(
    "answer",
    [
        {"answers": {QUESTION: ["SQLite"]}, "approved": "yes"},  # truthy str, not a bool
        {"answers": {QUESTION: 1}},  # an index where the CLI wants a label
        {"answers": {QUESTION: ["SQLite", 2]}},  # one bad element in a multiSelect
        {"answers": {QUESTION: []}},  # multiSelect that selected nothing
        {"answers": {}},  # "answered", with nothing to hand back
        {"text": ["not", "a", "string"]},
        {"sources": {QUESTION: 3}},
    ],
)
def test_answers_of_the_wrong_SHAPE_are_refused_not_just_the_wrong_keys(
    con: sqlite3.Connection, answer: dict
) -> None:
    # This dict becomes updated_input["answers"]. A type error here is caught by
    # the CLI's validator at best and misread as a different decision at worst.
    r = mk(con)
    with pytest.raises(ValueError):
        rq.answer_request(con, r.id, answer, "desk", "voice")  # type: ignore[arg-type]
    assert rq.get_request(con, r.id).state == "pending"  # type: ignore[union-attr]


def test_an_invented_answer_mode_is_refused(con: sqlite3.Connection) -> None:
    # 'was this a human or a timeout' is answered from this column.
    r = mk(con)
    with pytest.raises(ValueError):
        rq.answer_request(con, r.id, {"approved": True}, "desk", "telegram")  # type: ignore[arg-type]
    assert rq.get_request(con, r.id).state == "pending"  # type: ignore[union-attr]


def test_a_presentation_missing_a_field_a_channel_renders_is_refused(
    con: sqlite3.Connection,
) -> None:
    # The KeyError would otherwise happen in the Telegram process, hours later.
    broken = {k: v for k, v in PRES.items() if k != "allows_free_text"}
    with pytest.raises(ValueError):
        mk(con, presentation=broken)


def test_a_hand_built_presentation_with_a_bad_default_is_refused_at_creation(
    con: sqlite3.Connection,
) -> None:
    # make_presentation validates it; a hand-built dict must not get a pass, or
    # the failure surfaces inside the 3am expiry sweep instead.
    broken = dict(PRES)
    broken["default_answer"] = {"response": "SQLite"}
    with pytest.raises(ValueError):
        mk(con, presentation=broken, on_timeout="default")


def test_one_poisoned_default_does_not_abort_the_whole_sweep(con: sqlite3.Connection) -> None:
    """A row written before that check existed must not stop later deadlines.

    The sweep is a loop over every due row. A raise halfway through leaves every
    request behind it pending forever, and the symptom — timeouts silently
    stopping — is close to undiagnosable from the outside.
    """
    poisoned = past(con, on_timeout="default", tool_use_id="toolu_poison")
    con.execute(
        "UPDATE requests SET presentation=? WHERE id=?",
        (
            '{"verbatim":true,"intro":"q","items":[],"multi":false,'
            '"allows_free_text":true,"free_text_prompt":"","default_answer":{"approved":"yes"}}',
            poisoned.id,
        ),
    )
    healthy = past(con, on_timeout="deny", payload={"questions": ["other"]})

    by_id = {e.request_id: e for e in rq.expire_due(con)}
    assert set(by_id) == {poisoned.id, healthy.id}
    assert by_id[poisoned.id].outcome == "expired"
    assert "malformed" in by_id[poisoned.id].reason
    assert by_id[healthy.id].outcome == "denied"
    # And the poisoned row is closed, not left pending to fire again forever.
    assert rq.expire_due(con) == []
