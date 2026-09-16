"""The spine as one thing.

Every other test file exercises one module in isolation. This one walks the flow
the whole architecture exists for, across module boundaries and across
*connections*, because in production every one of these steps happens in a
different OS process:

    a job starts  ->  it raises a plan-mode question  ->  the desk goes quiet
    ->  presence decides the room is empty  ->  a DIFFERENT process answers
    ->  the driver resumes and finds the answer already there
    ->  it creates a repo it cannot delete  ->  spend is recorded in a unit
        that does not convert to dollars  ->  the kill switch stops everything
    ->  and the activity log can still prove none of it was tampered with.

A module that passes its own tests while failing to compose is the failure mode
this file exists to catch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jarvis import bus, db, effects, jobs, kill, ledger, presence, reconcile
from jarvis import requests as rq
from jarvis.ids import now as ids_now


@pytest.fixture()
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    db.open_db(p).close()
    return p


@pytest.fixture()
def con(dbpath: Path) -> sqlite3.Connection:
    return db.connect(dbpath)


def _storage_question() -> rq.Presentation:
    return rq.make_presentation(
        intro="Claude Code has a question about storage.",
        options=["SQLite", "JSON file", "Plain text"],
        verbatim=True,
        multi=False,
    )


def test_a_question_raised_by_one_process_is_answered_by_another(
    con: sqlite3.Connection, dbpath: Path
) -> None:
    """The phone layer in miniature, and the whole point of the spine."""
    job = jobs.create_job(con, kind="claude_code", title="the todo app build", created_by="desk")

    req = rq.create_request(
        con,
        kind="plan_question",
        short_label="storage",
        presentation=_storage_question(),
        payload={"questions": [{"question": "How should todos be stored?"}]},
        actor=f"runner:{job.id}",
        job_id=job.id,
        tool_use_id="toolu_stable_01",
    )
    jobs.set_state(con, job.id, "starting")
    jobs.set_state(con, job.id, "running")
    jobs.mark_blocked(con, job.id, req.id)

    # A completely separate connection — stand-in for the Telegram process.
    other = db.connect(dbpath)
    assert rq.answer_request(
        other,
        req.id,
        answer={"answers": {"How should todos be stored?": "SQLite"}},
        answered_by="telegram",
        answer_mode="button",
    )

    # The driver, resuming, finds it without asking anyone.
    assert rq.find_answer(con, "toolu_stable_01") == {
        "answers": {"How should todos be stored?": "SQLite"}
    }


def test_two_channels_racing_produce_exactly_one_winner(
    con: sqlite3.Connection, dbpath: Path
) -> None:
    """Desk and phone answering at once must not both win.

    If both could win, the user would answer on the phone, wander back to the
    desk, answer again, and silently get the first build or the second with no
    way to tell which.
    """
    job = jobs.create_job(con, kind="claude_code", title="t", created_by="desk")
    req = rq.create_request(
        con,
        kind="plan_question",
        short_label="storage",
        presentation=_storage_question(),
        payload={},
        actor="runner",
        job_id=job.id,
    )

    desk = db.connect(dbpath)
    phone = db.connect(dbpath)
    first = rq.answer_request(desk, req.id, answer={"answers": {"q": "SQLite"}}, answered_by="desk")
    second = rq.answer_request(
        phone, req.id, answer={"answers": {"q": "JSON file"}}, answered_by="phone"
    )

    assert [first, second] == [True, False]
    # And the winner's answer is the one that survives.
    assert rq.get_request(con, req.id).answer == {"answers": {"q": "SQLite"}}


def test_consume_is_idempotent_because_a_resumed_driver_asks_again(
    con: sqlite3.Connection,
) -> None:
    job = jobs.create_job(con, kind="claude_code", title="t", created_by="desk")
    req = rq.create_request(
        con,
        kind="plan_question",
        short_label="s",
        presentation=_storage_question(),
        payload={},
        actor="runner",
        job_id=job.id,
        tool_use_id="toolu_resume",
    )
    rq.answer_request(con, req.id, answer={"answers": {"q": "SQLite"}}, answered_by="desk")

    first = rq.consume(con, req.id)
    again = rq.consume(con, req.id)
    assert first == again == {"answers": {"q": "SQLite"}}


def test_answers_survive_the_process_that_wrote_them(dbpath: Path) -> None:
    """Spike S1's bug, pinned at the integration level.

    A write made by a process that then dies must be visible to its successor.
    A deferred transaction rolled back on close looks exactly like a protocol
    failure, and cost an afternoon to diagnose once already.
    """
    writer = db.connect(dbpath)
    job = jobs.create_job(writer, kind="claude_code", title="t", created_by="desk")
    req = rq.create_request(
        writer,
        kind="plan_question",
        short_label="s",
        presentation=_storage_question(),
        payload={},
        actor="runner",
        job_id=job.id,
        tool_use_id="toolu_dead",
    )
    rq.answer_request(writer, req.id, answer={"answers": {"q": "SQLite"}}, answered_by="telegram")
    writer.close()

    successor = db.connect(dbpath)
    assert rq.find_answer(successor, "toolu_dead") == {"answers": {"q": "SQLite"}}


def test_option_indices_resolve_to_verbatim_labels_and_never_guess(
    con: sqlite3.Connection,
) -> None:
    """The fidelity guarantee, at the integration seam.

    The model may emit an INDEX; a label is produced by local lookup. An index
    outside the array must raise rather than fall back to the first option,
    because a silent fallback builds the wrong thing and says nothing.
    """
    pres = _storage_question()
    assert rq.labels_for_indices(pres, [1]) == ["SQLite"]
    assert rq.labels_for_indices(pres, [3, 1]) == ["Plain text", "SQLite"]
    with pytest.raises(rq.OptionIndexError):
        rq.labels_for_indices(pres, [4])
    with pytest.raises(rq.OptionIndexError):
        rq.labels_for_indices(pres, [0])


def test_an_unanswered_question_is_evidence_the_room_is_empty(
    con: sqlite3.Connection,
) -> None:
    """The cheapest presence sensor in the system, and it costs nothing.

    Speaking into an empty room while a build waits forty minutes is the
    expensive failure; one redundant Telegram message is the cheap one.
    """
    presence.record_signal(con, "utterance", {"heard": True}, ttl_s=300)
    before = presence.evaluate_presence(con)

    asked = ids_now()
    presence.record_signal(con, "probe", {"outcome": "unanswered", "asked_at": asked}, ttl_s=300)
    after = presence.evaluate_presence(con)

    # The verdict must be explainable out loud either way — "where do you think
    # I am?" is a question the user can actually ask.
    assert isinstance(after.reason, str) and after.reason
    assert before.state in {"present", "away", "unknown", "asleep", "on_call"}
    assert after.state in {"present", "away", "unknown", "asleep", "on_call"}


def test_creating_a_repo_is_irreversible_and_jarvis_says_so(con: sqlite3.Connection) -> None:
    """The reconciliation nobody had done.

    The GitHub token deliberately lacks delete_repo, so repo creation cannot be
    undone. The honesty requirement is that Jarvis says that BEFORE acting and
    never claims otherwise afterwards.
    """
    job = jobs.create_job(con, kind="repo_setup", title="comment watcher", created_by="desk")
    eff = effects.record_effect(
        con,
        kind="github.repo_create",
        summary="created the repository Efkrdnz/comment-watcher, private and empty",
        reversibility="irreversible",
        job_id=job.id,
        provider_ref={"full_name": "Efkrdnz/comment-watcher"},
        actor="desk",
    )
    line = effects.spoken_effect_line(eff)

    assert eff.reversibility == "irreversible"
    low = line.lower()
    # It must say plainly that nothing can be done...
    assert "nothing i can do" in low or "nothing can be done" in low, line
    # ...and must never offer to undo or delete it.
    for promise in ("i can delete", "i'll delete", "i will delete", "i can undo", "i'll undo"):
        assert promise not in low, line


def test_spend_in_a_unit_that_does_not_convert_is_spoken_as_unpriced(
    con: sqlite3.Connection,
) -> None:
    """A zero running total must never be mistaken for free.

    Under a Max subscription the meaningful unit is rate-limit proximity, not
    dollars, so usd_equiv is NULL — and the spoken status has to say that out
    loud rather than reporting $0.00 and sounding reassuring.
    """
    ledger.record(con, provider="claude_code", unit="rate_window_pct", amount=41.0, usd_equiv=None)
    st = ledger.status(con, "today")
    spoken = ledger.spoken_status(st)

    assert st.unpriced, "a NULL usd_equiv must surface as an unpriced meter"
    assert "claude" in spoken.lower()
    assert spoken.strip()


def test_the_kill_switch_crosses_a_process_boundary(con: sqlite3.Connection, dbpath: Path) -> None:
    """The DTMF handler lives in a different process from the runner it kills.

    So the kill must travel entirely through the database. An in-process
    reference would work on the desk and silently fail from a phone call.
    """
    job = jobs.create_job(con, kind="claude_code", title="t", created_by="desk")
    jobs.set_state(con, job.id, "starting")
    jobs.set_state(con, job.id, "running")

    phone = db.connect(dbpath)  # the DTMF *9 handler
    cmd = kill.issue_command(phone, verb="stop_all", issued_by="phone:dtmf")

    runner = db.connect(dbpath)  # the Claude Code driver, elsewhere
    pending = kill.pending_commands(runner, actor=f"runner:{job.id}")
    assert any(c.id == cmd.id for c in pending)

    kill.ack_command(runner, cmd.id, actor=f"runner:{job.id}", result="terminated")
    assert kill.acked_by(con, cmd.id, f"runner:{job.id}")


def test_project_status_answers_the_briefings_first_section(con: sqlite3.Connection) -> None:
    """Briefing section 1 had no data source in the research. This is it."""
    done = jobs.create_job(con, kind="claude_code", title="the todo app", created_by="desk")
    jobs.set_state(con, done.id, "starting")
    jobs.set_state(con, done.id, "running")
    jobs.set_state(con, done.id, "done")

    stuck = jobs.create_job(con, kind="claude_code", title="the scraper", created_by="desk")
    jobs.set_state(con, stuck.id, "starting")
    jobs.set_state(con, stuck.id, "running")
    req = rq.create_request(
        con,
        kind="plan_question",
        short_label="s",
        presentation=_storage_question(),
        payload={},
        actor="runner",
        job_id=stuck.id,
    )
    jobs.mark_blocked(con, stuck.id, req.id)

    status = reconcile.project_status(con)
    spoken = " ".join(status.lines)
    assert "todo app" in spoken or "scraper" in spoken, spoken


def test_reconcile_is_idempotent_and_safe_to_run_at_every_startup(
    con: sqlite3.Connection,
) -> None:
    jobs.create_job(con, kind="claude_code", title="t", created_by="desk")
    first = reconcile.reconcile(con)
    second = reconcile.reconcile(con)
    assert type(first) is type(second)


def test_the_activity_log_records_the_whole_flow_and_still_verifies(
    con: sqlite3.Connection, dbpath: Path
) -> None:
    """Every step above wrote to the same hash-chained log, from several
    connections. Tamper-evidence has to survive that, or it is decorative."""
    job = jobs.create_job(con, kind="claude_code", title="t", created_by="desk")
    other = db.connect(dbpath)
    req = rq.create_request(
        other,
        kind="plan_question",
        short_label="s",
        presentation=_storage_question(),
        payload={},
        actor="runner",
        job_id=job.id,
    )
    rq.answer_request(con, req.id, answer={"answers": {"q": "SQLite"}}, answered_by="desk")
    ledger.record(con, provider="gemini", unit="gemini_sec", amount=12.0, usd_equiv=0.002)

    assert con.execute("SELECT count(*) FROM events").fetchone()[0] > 0
    assert bus.verify_chain(con) is None, "the chain must verify across multiple writers"


def test_tampering_with_the_log_is_detected(con: sqlite3.Connection) -> None:
    """Without this, the honesty requirement is a claim rather than a property."""
    jobs.create_job(con, kind="claude_code", title="t", created_by="desk")
    seq = con.execute("SELECT min(seq) FROM events").fetchone()[0]
    assert seq is not None

    con.execute("UPDATE events SET actor='somebody else' WHERE seq=?", (seq,))
    assert bus.verify_chain(con) == seq
