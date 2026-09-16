"""``can_use_tool``, tested against the payload shapes spike S1 recorded.

No live call anywhere in this file. The seam is the SDK's own types — they are
plain dataclasses, so a fake ``ToolPermissionContext`` and a real
:class:`PermissionResultAllow` assertion prove the exact bytes the CLI would
receive without a credential, a network or a subprocess.

The answers are written on a SECOND CONNECTION throughout, because that is what
the phone worker and the Telegram bot actually are: another process, writing the
row this one is polling.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from jarvis import jobs, kill
from jarvis import requests as rq
from jarvis.bus import read_since
from jarvis.cc import policy
from jarvis.cc.hooks import DeferLedger
from jarvis.cc.permission_host import HEARTBEAT_S, PermissionHost
from jarvis.db import connect, migrate

JOB = "job_hosthosthos"
Q = "How should todos be stored?"
QF = "Which features should be included in the tiny todo CLI?"

SINGLE = {
    "questions": [
        {
            "header": "Storage",
            "question": Q,
            "options": [
                {"label": "SQLite", "description": "a local database file"},
                {"label": "JSON file", "description": "a single JSON file"},
            ],
            "multiSelect": False,
        }
    ]
}
MULTI = {
    "questions": [
        {
            "header": "Features",
            "question": QF,
            "options": [
                {"label": "Due dates"},
                {"label": "Tags"},
                {"label": "Priorities"},
            ],
            "multiSelect": True,
        }
    ]
}
PLAN = {"plan": "1. Create the repo\n2. Add a SQLite store\n3. Write tests"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    con = connect(p)
    migrate(con)
    jobs.create_job(
        con,
        kind="claude_code",
        title="the todo app build",
        created_by="test",
        job_id=JOB,
        state="running",
        cwd=str(tmp_path),
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
    """The answering process: Telegram, the phone worker, a resumed driver."""
    c = connect(db_path)
    yield c
    c.close()


def ctx(tool_use_id: str | None = "toolu_01") -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=tool_use_id)


def answerer(other: sqlite3.Connection, answer: rq.Answer, by: str = "telegram"):
    """A sleep that answers the pending question the first time it is awaited.

    Injected in place of ``asyncio.sleep`` so the cross-process answer lands at a
    known point in the poll loop instead of at a hopeful wall-clock offset.
    """
    state = {"done": False}

    async def _sleep(_seconds: float) -> None:
        if state["done"]:
            return
        state["done"] = True
        pending = rq.open_requests(other, JOB)
        assert pending, "the host should have created a row before it started waiting"
        assert rq.answer_request(other, pending[0].id, answer, by, "button")

    return _sleep


async def never_sleeps(_seconds: float) -> None:
    raise AssertionError("the host waited when it should have answered from the database")


# ───────────────────────── the recorded payload shapes ─────────────────────────


async def test_single_select_reaches_the_cli_as_one_label_string(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, sleep=answerer(other, {"answers": {Q: "SQLite"}}))
    result = await host("AskUserQuestion", SINGLE, ctx())

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {**SINGLE, "answers": {Q: "SQLite"}}


async def test_multiselect_reaches_the_cli_as_a_list_of_labels(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    answer: rq.Answer = {"answers": {QF: ["Due dates", "Priorities"]}}
    host = PermissionHost(con, JOB, sleep=answerer(other, answer))
    result = await host("AskUserQuestion", MULTI, ctx("toolu_multi"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {QF: ["Due dates", "Priorities"]}


async def test_none_of_these_reaches_the_cli_as_the_users_own_words(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    answer: rq.Answer = {"answers": {Q: "Postgres, actually"}, "sources": {Q: "free_text"}}
    host = PermissionHost(con, JOB, sleep=answerer(other, answer))
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_free"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {Q: "Postgres, actually"}


async def test_response_is_never_emitted_alongside_answers(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, sleep=answerer(other, {"answers": {Q: "SQLite"}}))
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_resp"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    # S1, measured: with 'response' present the CLI shows Claude "The user
    # responded: ..." and the per-question answers are silently discarded.
    assert "response" not in result.updated_input
    # And every original field is echoed byte-identical: the CLI's validator
    # refuses a changed shown field.
    assert result.updated_input["questions"] == SINGLE["questions"]


async def test_every_original_input_field_survives_untouched(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    payload = {**SINGLE, "someFutureField": {"a": 1}}
    host = PermissionHost(con, JOB, sleep=answerer(other, {"answers": {Q: "SQLite"}}))
    result = await host("AskUserQuestion", payload, ctx("toolu_extra"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["someFutureField"] == {"a": 1}


# ───────────────────────────── the resume path ─────────────────────────────


async def test_an_answer_written_while_this_process_was_dead_returns_without_waiting(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # Exactly the S1 scenario-4 shape: the row and the answer exist, written by
    # somebody else, and THIS process has never seen the question before.
    from jarvis.cc.gate import ensure_request

    req = ensure_request(
        other,
        job_id=JOB,
        actor="runner:dead",
        tool_name="AskUserQuestion",
        tool_use_id="toolu_resumed",
        input_data=SINGLE,
    )
    assert rq.answer_request(other, req.id, {"answers": {Q: "JSON file"}}, "phone", "dtmf")

    host = PermissionHost(con, JOB, sleep=never_sleeps)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_resumed"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {Q: "JSON file"}
    assert rq.get_request(con, req.id).state == "consumed"  # type: ignore[union-attr]


async def test_a_second_resume_replays_the_same_answer_rather_than_re_asking(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    from jarvis.cc.gate import ensure_request

    req = ensure_request(
        other,
        job_id=JOB,
        actor="runner:dead",
        tool_name="AskUserQuestion",
        tool_use_id="toolu_twice",
        input_data=SINGLE,
    )
    rq.answer_request(other, req.id, {"answers": {Q: "SQLite"}}, "desk", "voice")

    host = PermissionHost(con, JOB, sleep=never_sleeps)
    first = await host("AskUserQuestion", SINGLE, ctx("toolu_twice"))
    # Consumed once already. A resumed runner re-fires the same question, and
    # consumption being idempotent is what stops that being an error.
    second = await host("AskUserQuestion", SINGLE, ctx("toolu_twice"))
    assert isinstance(first, PermissionResultAllow)
    assert isinstance(second, PermissionResultAllow)
    assert first.updated_input == second.updated_input


async def test_the_answer_really_crosses_two_connections_under_real_concurrency(
    con: sqlite3.Connection, db_path: Path
) -> None:
    """No injected sleep: a real thread, a real second connection, a real poll."""
    started = threading.Event()

    def answer_from_another_process() -> None:
        started.wait(2.0)
        writer = connect(db_path)
        try:
            for _ in range(200):
                pending = rq.open_requests(writer, JOB)
                if pending:
                    rq.answer_request(
                        writer, pending[0].id, {"answers": {Q: "SQLite"}}, "phone", "voice"
                    )
                    return
                threading.Event().wait(0.01)
        finally:
            writer.close()

    thread = threading.Thread(target=answer_from_another_process)
    thread.start()
    host = PermissionHost(con, JOB, poll_s=0.01)
    started.set()
    result = await asyncio.wait_for(host("AskUserQuestion", SINGLE, ctx("toolu_thread")), 10)
    thread.join(5)

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {Q: "SQLite"}


# ───────────────────────── the ways a wait must end ─────────────────────────


async def test_a_cancelled_question_denies_instead_of_blocking_forever(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    async def cancel(_seconds: float) -> None:
        pending = rq.open_requests(other, JOB)
        if pending:
            rq.cancel_request(other, pending[0].id)

    host = PermissionHost(con, JOB, sleep=cancel)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_cancel"))

    assert isinstance(result, PermissionResultDeny)
    assert "do not assume" in result.message


async def test_an_expired_question_denies_with_the_spines_own_sentence(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    async def expire(_seconds: float) -> None:
        pending = rq.open_requests(other, JOB)
        for req in pending:
            other.execute(
                "UPDATE requests SET expires_at=?, on_timeout='deny' WHERE id=?",
                ("2000-01-01T00:00:00.000Z", req.id),
            )
        rq.expire_due(other)

    host = PermissionHost(con, JOB, sleep=expire)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_expire"))

    # on_timeout='deny' writes a denial through the same CAS a human would use,
    # so the waiting driver reads it as an ANSWER and passes it on as a refusal —
    # in the spine's own words, which are the words the activity log shows.
    assert isinstance(result, PermissionResultDeny)
    assert result.message == rq.TIMEOUT_DENY_TEXT


async def test_the_kill_switch_ends_a_wait_that_nothing_else_would_end(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    async def fire_the_kill_switch(_seconds: float) -> None:
        kill.bump_epoch(other, actor="voice", reason="user said stop")

    host = PermissionHost(con, JOB, sleep=fire_the_kill_switch)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_killed"))

    assert isinstance(result, PermissionResultDeny)
    # interrupt=True: a killed session must stop, not politely finish its turn.
    assert result.interrupt is True
    assert "stopped by the user" in result.message


async def test_a_job_that_someone_else_finished_stops_the_wait(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    async def finish_it_elsewhere(_seconds: float) -> None:
        jobs.set_state(other, JOB, "killed", actor="dispatch")

    host = PermissionHost(con, JOB, sleep=finish_it_elsewhere)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_gone"))

    assert isinstance(result, PermissionResultDeny)
    assert "killed" in result.message


async def test_the_first_answer_wins_and_the_host_takes_the_winners(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    async def two_channels_race(_seconds: float) -> None:
        req = rq.open_requests(other, JOB)[0]
        assert rq.answer_request(other, req.id, {"answers": {Q: "SQLite"}}, "desk", "voice")
        # The loser is TOLD it lost rather than failing silently, and the answer
        # the host uses is the winner's.
        assert not rq.answer_request(other, req.id, {"answers": {Q: "JSON file"}}, "phone", "dtmf")

    host = PermissionHost(con, JOB, sleep=two_channels_race)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_race"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {Q: "SQLite"}


# ───────────────────── answers written by somebody who is not us ─────────────────────


async def test_an_invented_label_from_another_channel_never_reaches_claude(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    # The answer was written hours ago by a process that does not import jarvis.answers.
    # This is the last place it can be checked against the frozen options array.
    host = PermissionHost(con, JOB, sleep=answerer(other, {"answers": {Q: "Postgres"}}))
    bad = await host("AskUserQuestion", MULTI, ctx("toolu_invented"))
    assert isinstance(bad, PermissionResultDeny)
    assert "does not fit" in bad.message


async def test_a_multi_answer_shaped_as_a_single_one_is_refused(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, sleep=answerer(other, {"answers": {QF: "Due dates"}}))
    result = await host("AskUserQuestion", MULTI, ctx("toolu_shape"))
    assert isinstance(result, PermissionResultDeny)


async def test_an_approval_with_no_per_question_answer_is_not_an_answer(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, sleep=answerer(other, {"approved": True}))
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_empty"))
    assert isinstance(result, PermissionResultDeny)
    assert "No per-question answer" in result.message


async def test_a_question_payload_that_cannot_be_narrated_is_denied_not_crashed(
    con: sqlite3.Connection,
) -> None:
    host = PermissionHost(con, JOB, sleep=never_sleeps)
    result = await host("AskUserQuestion", {"questions": []}, ctx("toolu_junk"))
    assert isinstance(result, PermissionResultDeny)
    assert rq.open_requests(con, JOB) == []


# ───────────────────────────── ExitPlanMode ─────────────────────────────


async def test_an_approved_plan_goes_back_unchanged(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, sleep=answerer(other, {"approved": True}))
    result = await host("ExitPlanMode", PLAN, ctx("toolu_plan_ok"))

    assert isinstance(result, PermissionResultAllow)
    # The plan Claude wrote is the plan that was approved. Editing it here would
    # approve a different one.
    assert result.updated_input == PLAN


async def test_a_refused_plan_sends_claude_back_to_planning_with_the_reason(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    answer: rq.Answer = {"approved": False, "text": "no, change the storage"}
    host = PermissionHost(con, JOB, sleep=answerer(other, answer))
    result = await host("ExitPlanMode", PLAN, ctx("toolu_plan_no"))

    assert isinstance(result, PermissionResultDeny)
    assert "keep planning" in result.message.lower()
    assert "no, change the storage" in result.message
    assert result.interrupt is False


async def test_the_plan_row_carries_the_plan_text_for_whoever_presents_it(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, sleep=answerer(other, {"approved": True}))
    await host("ExitPlanMode", PLAN, ctx("toolu_plan_row"))

    row = rq.get_request(con, rq.find_open_for_tool(other, JOB, "toolu_plan_row", "x", {}).id)  # type: ignore[union-attr]
    assert row is not None
    assert row.kind == "exit_plan"
    assert row.payload == PLAN
    assert row.presentation["intro"] == PLAN["plan"]
    assert [i["label"] for i in row.presentation["items"]] == ["Approve", "Keep planning"]


# ───────────────────────────── the policy path ─────────────────────────────


async def test_the_phone_denies_a_shell_without_asking_anybody(
    con: sqlite3.Connection,
) -> None:
    host = PermissionHost(con, JOB, channel="phone", sleep=never_sleeps)
    result = await host("Bash", {"command": "rm -rf /"}, ctx("toolu_bash"))

    assert isinstance(result, PermissionResultDeny)
    # Nobody is woken at 2am to refuse something policy already refuses.
    assert rq.open_requests(con, JOB) == []
    assert "tool.denied" in [e.kind for e in read_since(con, 0)]


async def test_an_allowed_tool_is_recorded_on_the_bus_too(con: sqlite3.Connection) -> None:
    host = PermissionHost(con, JOB, channel="desk", sleep=never_sleeps)
    result = await host("Read", {"file_path": "main.py"}, ctx("toolu_read"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is None  # nothing to rewrite; allow it as-is
    events = [e for e in read_since(con, 0) if e.kind == "tool.used"]
    assert events and events[-1].payload["decision"] == "allow"


async def test_an_ask_policy_raises_a_real_question_and_honours_the_answer(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(con, JOB, channel="desk", sleep=answerer(other, {"approved": True}))
    result = await host("Bash", {"command": "pytest -q"}, ctx("toolu_ask_ok"))
    assert isinstance(result, PermissionResultAllow)

    denier = PermissionHost(con, JOB, channel="desk", sleep=answerer(other, {"approved": False}))
    denied = await denier("Bash", {"command": "git push --force"}, ctx("toolu_ask_no"))
    assert isinstance(denied, PermissionResultDeny)
    assert "did not approve" in denied.message


async def test_a_missing_decision_is_a_denial_and_never_an_approval(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    host = PermissionHost(
        con, JOB, channel="desk", sleep=answerer(other, {"text": "hmm, maybe later"})
    )
    result = await host("Bash", {"command": "make deploy"}, ctx("toolu_ask_vague"))
    assert isinstance(result, PermissionResultDeny)


async def test_an_unknown_channel_cannot_run_anything(con: sqlite3.Connection) -> None:
    host = PermissionHost(con, JOB, channel="dekstop", sleep=never_sleeps)
    result = await host("Read", {"file_path": "main.py"}, ctx("toolu_typo"))
    assert isinstance(result, PermissionResultDeny)


async def test_a_custom_policy_table_is_honoured_over_the_default(
    con: sqlite3.Connection,
) -> None:
    table = {"kiosk": policy.ChannelPolicy(name="kiosk", default="allow", deny_secret_paths=True)}
    host = PermissionHost(con, JOB, channel="kiosk", policies=table, sleep=never_sleeps)
    assert isinstance(await host("Bash", {"command": "ls"}, ctx("toolu_k1")), PermissionResultAllow)
    secret = await host("Read", {"file_path": "~/.ssh/id_rsa"}, ctx("toolu_k2"))
    assert isinstance(secret, PermissionResultDeny)


# ───────────────────────────── the dropped defer ─────────────────────────────


async def test_a_defer_the_cli_ignored_falls_through_to_blocking_and_says_so(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    ledger = DeferLedger()
    ledger.add("toolu_dropped")  # the hook asked; the CLI dropped it
    host = PermissionHost(
        con, JOB, deferrals=ledger, sleep=answerer(other, {"answers": {Q: "SQLite"}})
    )
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_dropped"))

    assert isinstance(result, PermissionResultAllow)
    blocked = [e for e in read_since(con, 0) if e.kind == "job.blocked"]
    assert any(e.payload.get("defer_rejected") for e in blocked)
    # Recorded once, then forgotten: the same id re-fires after an HONOURED
    # defer too, and a sticky flag would cry wolf on every resume.
    assert "toolu_dropped" not in ledger


async def test_the_job_is_parked_on_the_question_and_released_after_the_answer(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    seen: list[str] = []

    async def peek_then_answer(_seconds: float) -> None:
        job = jobs.get(other, JOB)
        assert job is not None
        seen.append(job.state)
        assert job.blocked_request_id is not None
        req = rq.open_requests(other, JOB)[0]
        rq.answer_request(other, req.id, {"answers": {Q: "SQLite"}}, "desk", "voice")

    host = PermissionHost(con, JOB, sleep=peek_then_answer)
    await host("AskUserQuestion", SINGLE, ctx("toolu_block"))

    # Another process can SEE that this job is waiting on a question, which is
    # what the morning briefing reads out.
    assert seen == ["blocked"]
    job = jobs.get(con, JOB)
    assert job is not None and job.state == "running"
    assert job.blocked_request_id is None


async def test_the_same_command_asked_twice_is_asked_twice(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """A yes given once must not approve the same call again an hour later.

    find_open_for_tool skips CONSUMED rows on purpose, but the attempt counter
    has to move with it: creating the second ask at attempt=1 would land on the
    first row through UNIQUE(job_id, dedupe_key, attempt) and hand back the
    answer the user gave to a different invocation.
    """
    command = {"command": "rm -rf build"}
    first = PermissionHost(con, JOB, channel="desk", sleep=answerer(other, {"approved": True}))
    assert isinstance(await first("Bash", command, ctx("toolu_rm1")), PermissionResultAllow)

    asked_again = {"n": 0}

    async def refuse_the_second_time(_seconds: float) -> None:
        asked_again["n"] += 1
        pending = rq.open_requests(other, JOB)
        assert pending, "the second identical call must raise its own question"
        rq.answer_request(other, pending[0].id, {"approved": False}, "desk", "voice")

    second = PermissionHost(con, JOB, channel="desk", sleep=refuse_the_second_time)
    result = await second("Bash", command, ctx("toolu_rm2"))

    assert asked_again["n"] == 1
    assert isinstance(result, PermissionResultDeny)
    rows = con.execute(
        "SELECT attempt FROM requests WHERE job_id=? AND kind='tool_permission' ORDER BY attempt",
        (JOB,),
    ).fetchall()
    assert [r["attempt"] for r in rows] == [1, 2]


# ───────────────────────── the heartbeat during a long wait ─────────────────────────


async def test_a_blocked_runner_keeps_beating_even_at_a_slow_poll(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """'blocked' is an ACTIVE state, so a parked runner must keep saying it exists.

    The throttle used to be ``tick % ticks_per_beat == 1``, which is never true
    when ticks_per_beat is 1 — i.e. for ANY poll_s at or above HEARTBEAT_S, the
    exact setting an operator reaches for to cut write pressure during a
    ninety-minute wait. The job then stopped beating entirely, reconcile called a
    perfectly healthy blocked runner orphaned, and respawned the build under it.
    """
    con.execute("UPDATE jobs SET heartbeat_at=NULL WHERE id=?", (JOB,))
    assert jobs.get(con, JOB).heartbeat_at is None

    # The tick budget is fixed and the answer lands unconditionally, so a host
    # that never beats FAILS here rather than polling this test forever.
    seen: list[str | None] = []

    async def three_ticks_then_answer(_seconds: float) -> None:
        seen.append(jobs.get(other, JOB).heartbeat_at)
        if len(seen) >= 3:
            pending = rq.open_requests(other, JOB)
            assert rq.answer_request(
                other, pending[0].id, {"answers": {Q: "SQLite"}}, "tg", "button"
            )

    host = PermissionHost(con, JOB, poll_s=HEARTBEAT_S, sleep=three_ticks_then_answer)
    result = await host("AskUserQuestion", SINGLE, ctx("toolu_beat"))

    assert isinstance(result, PermissionResultAllow)
    assert any(s is not None for s in seen), (
        f"a runner blocked at poll_s={HEARTBEAT_S} never stamped a heartbeat: {seen}"
    )
    assert jobs.get(other, JOB).heartbeat_at is not None
