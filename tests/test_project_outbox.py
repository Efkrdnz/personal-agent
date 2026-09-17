"""The at-most-once row, tested where it breaks: races, restarts, and repair scripts.

Repository creation is the second ``at_most_once`` op in this system (the first is
``phone.dial``), and the property is not "usually only once". Three ways a second
attempt actually happens are each covered here: two processes racing, a process
that died mid-call, and a well-meaning human resetting the row to pending.

RACES use two real connections to one file, because SQLite locking is
per-connection rather than per-process: two connections racing on one file
exercise exactly what two daemons would.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.db import connect, migrate
from jarvis.project import outbox as ob

FN = "Efkrdnz/comment-watcher"
KEY = f"{ob.REPO_CREATE_OP}:{FN.lower()}"
ARGS = {"owner": "Efkrdnz", "name": "comment-watcher", "full_name": FN}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


def enqueue(con: sqlite3.Connection) -> ob.OutboxRow:
    return ob.enqueue(con, op=ob.REPO_CREATE_OP, args=ARGS, idem_key=KEY)


# ───────────────────────────── the row ─────────────────────────────


def test_a_repo_create_row_is_at_most_once_with_no_retry_budget(con: sqlite3.Connection) -> None:
    row = enqueue(con)
    assert row.op == ob.REPO_CREATE_OP
    assert row.at_most_once is True
    assert row.max_attempts == 1
    assert row.attempts == 0
    assert row.state == "pending"
    assert row.args["full_name"] == FN


def test_an_at_most_once_row_may_not_be_given_a_retry_budget(con: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="max_attempts must be 1"):
        ob.enqueue(
            con, op=ob.REPO_CREATE_OP, args=ARGS, idem_key=KEY, at_most_once=True, max_attempts=3
        )


def test_enqueueing_the_same_repository_twice_is_one_row(con: sqlite3.Connection) -> None:
    """The yes that authorised this can be consumed twice by a resumed process."""
    first = enqueue(con)
    again = enqueue(con)
    assert again.id == first.id
    count = con.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
    assert count == 1


def test_a_row_written_here_is_readable_from_a_process_started_later(db_path: Path) -> None:
    writer = connect(db_path)
    row = enqueue(writer)
    writer.close()

    reader = connect(db_path)
    found = ob.get(reader, row.id)
    assert found is not None and found.args == ARGS
    assert [r.id for r in ob.pending(reader, op=ob.REPO_CREATE_OP)] == [row.id]
    reader.close()


# ───────────────────────────── attempting it ─────────────────────────────


def test_beginning_the_call_spends_the_row(con: sqlite3.Connection) -> None:
    row = enqueue(con)
    assert ob.begin(con, row.id, "desk") is True
    inflight = ob.get(con, row.id)
    assert inflight is not None
    assert inflight.state == "inflight"
    assert inflight.attempts == 1
    assert inflight.claimed_by == "desk"
    assert inflight.spent is True


def test_two_processes_racing_produce_exactly_one_attempt(db_path: Path) -> None:
    a = connect(db_path)
    row = enqueue(a)

    won: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def race() -> None:
        # Opened INSIDE the thread: sqlite3 refuses a connection across threads,
        # and this is the shape that matches the real thing anyway — two
        # processes, each with its own handle on one file.
        conn = connect(db_path)
        try:
            barrier.wait()
            outcome = ob.begin(conn, row.id, "racer")
        finally:
            conn.close()
        with lock:
            won.append(outcome)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(won) == [False, True]
    final = ob.get(a, row.id)
    assert final is not None and final.attempts == 1
    a.close()


def test_resetting_the_row_to_pending_does_not_buy_a_second_attempt(
    con: sqlite3.Connection,
) -> None:
    """The repair script somebody writes at 2am, refused by the attempt counter.

    The state column alone would let this through, and a second POST to the repos
    endpoint is a second repository.
    """
    row = enqueue(con)
    assert ob.begin(con, row.id, "desk")
    con.execute("UPDATE outbox SET state='pending' WHERE id=?", (row.id,))
    assert ob.begin(con, row.id, "desk") is False


def test_a_finished_row_records_the_effect_that_proves_it(con: sqlite3.Connection) -> None:
    row = enqueue(con)
    ob.begin(con, row.id, "desk")
    assert ob.done(con, row.id, result={"full_name": FN}, effect_id=None) is True
    done = ob.get(con, row.id)
    assert done is not None
    assert done.state == "done"
    assert done.result == {"full_name": FN}
    assert done.claim_expires_at is None
    # And it cannot be attempted again from any state.
    assert ob.begin(con, row.id, "desk") is False


def test_a_refusal_is_a_failure_and_not_a_question_for_a_human(con: sqlite3.Connection) -> None:
    """422 name-taken and 403 no-scope provably changed nothing. Nobody needs paging."""
    row = enqueue(con)
    ob.begin(con, row.id, "desk")
    assert ob.refused(con, row.id, "422 name already exists") is True
    failed = ob.get(con, row.id)
    assert failed is not None and failed.state == "failed"
    assert ob.needs_human(con) == []


# ───────────────────────────── when nobody can know ─────────────────────────────


def test_an_ambiguous_failure_waits_for_a_human_and_never_retries(
    con: sqlite3.Connection,
) -> None:
    row = enqueue(con)
    ob.begin(con, row.id, "desk")
    assert ob.escalate(con, row.id, "TransportError: connection reset") is True
    escalated = ob.get(con, row.id)
    assert escalated is not None and escalated.state == "needs_human"
    assert [r.id for r in ob.needs_human(con)] == [row.id]
    assert ob.pending(con) == []
    assert ob.begin(con, row.id, "desk") is False


def test_what_jarvis_says_about_a_row_that_needs_a_human(con: sqlite3.Connection) -> None:
    """It names the repository, admits it does not know, and promises no retry."""
    row = enqueue(con)
    ob.begin(con, row.id, "desk")
    ob.escalate(con, row.id, "TransportError: reset")
    escalated = ob.get(con, row.id)
    assert escalated is not None
    said = ob.spoken_escalation(escalated)
    assert FN in said
    assert "don't know" in said
    assert "not going to ask again" in said
    for promise in ("I'll try again", "retrying", "I will retry"):
        assert promise.lower() not in said.lower()


def test_a_process_that_died_mid_call_is_reported_rather_than_retried(
    con: sqlite3.Connection,
) -> None:
    row = enqueue(con)
    ob.begin(con, row.id, "desk", lease_s=1)
    assert ob.stranded(con) == []  # the lease has not lapsed yet

    late = "2999-01-01T00:00:00.000Z"
    assert [r.id for r in ob.stranded(con, now_ts=late)] == [row.id]
    moved = ob.escalate_stranded(con, now_ts=late)
    assert [r.state for r in moved] == ["needs_human"]
    assert "died mid-call" in (moved[0].error or "")
    # Idempotent: a second sweep finds nothing, because the row left 'inflight'.
    assert ob.escalate_stranded(con, now_ts=late) == []


def test_a_retryable_row_is_not_swept_by_the_at_most_once_sweeper(
    con: sqlite3.Connection,
) -> None:
    """The sweeper is about the un-retryable ops only; a speak row is not its business."""
    row = ob.enqueue(
        con,
        op="speak",
        args={"text": "hello"},
        idem_key="speak:1",
        at_most_once=False,
        max_attempts=3,
    )
    assert ob.begin(con, row.id, "desk", lease_s=1)
    assert ob.stranded(con, now_ts="2999-01-01T00:00:00.000Z") == []
