"""The runner, with the SDK faked at the client seam.

The fake is a client, not a message type: ``AssistantMessage``, ``ResultMessage``
and ``DeferredToolUse`` are the SDK's own plain dataclasses and are constructed
here for real, so what these tests assert is the shape the CLI actually produces.
Nothing dials out; there is no credential in CI and none is needed.

The centrepiece is :func:`test_defer_then_answer_then_resume_across_two_runners`,
which is spike S1's scenario 4 — defer, process death, an answer written by
somebody else, a fresh process resuming — with the live CLI replaced and
everything else, including both SQLite connections, real.
"""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    DeferredToolUse,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from jarvis import jobs, kill
from jarvis import requests as rq
from jarvis.cc import driver as drv
from jarvis.cc.settings import AskUserQuestionTimeoutSet
from jarvis.db import connect, migrate

JOB = "job_drivedrived"
Q = "How should todos be stored?"
SINGLE = {
    "questions": [
        {
            "header": "Storage",
            "question": Q,
            "options": [{"label": "SQLite"}, {"label": "JSON file"}],
            "multiSelect": False,
        }
    ]
}


# ───────────────────────────── the fake seam ─────────────────────────────


class FakeClient:
    """Everything ``ClaudeJobRunner`` uses of ``ClaudeSDKClient``, and no more.

    A step is either a message to yield or a callable that gets this client — so
    a step can invoke ``options.can_use_tool`` exactly the way the CLI does, and
    the permission host runs for real inside the run loop.
    """

    def __init__(self, options: Any, steps: list[Any]) -> None:
        self.options = options
        self.steps = steps
        self.prompts: list[str] = []
        self.results: list[Any] = []
        self.closed = False

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.closed = True
        return False

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for step in self.steps:
            message = step(self) if callable(step) else step
            if inspect.isawaitable(message):
                message = await message
            if message is not None:
                yield message


def factory_for(steps: list[Any], made: list[FakeClient]) -> Callable[[Any], FakeClient]:
    def factory(options: Any) -> FakeClient:
        client = FakeClient(options, steps)
        made.append(client)
        return client

    return factory


def asks(input_data: dict, tool_use_id: str) -> Callable[[FakeClient], Any]:
    """A step that fires AskUserQuestion into the host, as the CLI would."""

    async def step(client: FakeClient) -> None:
        result = await client.options.can_use_tool(
            "AskUserQuestion", input_data, ToolPermissionContext(tool_use_id=tool_use_id)
        )
        client.results.append(result)

    return step


def result_message(**kw: Any) -> ResultMessage:
    base: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sess-abc",
        "stop_reason": "end_turn",
    }
    base.update(kw)
    return ResultMessage(**base)


def runner(
    con: sqlite3.Connection, steps: list[Any] | None = None, **kw: Any
) -> tuple[drv.ClaudeJobRunner, list[FakeClient]]:
    made: list[FakeClient] = []
    r = drv.ClaudeJobRunner(
        con,
        JOB,
        client_factory=factory_for(steps or [], made),
        # Always explicit: a test that read the developer's real ~/.claude would
        # pass or fail depending on whose laptop it ran on.
        settings_paths=[],
        **kw,
    )
    return r, made


# ───────────────────────────── fixtures ─────────────────────────────


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
        cwd=str(tmp_path),
        model="claude-opus-5",
        effort="xhigh",
        prompt_text="build me a todo cli",
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
    c = connect(db_path)
    yield c
    c.close()


# ───────────────────────────── options ─────────────────────────────


def test_askuserquestion_can_never_be_in_allowed_tools(con: sqlite3.Connection) -> None:
    r, _ = runner(con, [])
    with pytest.raises(ValueError, match="auto-approves"):
        # A whole-tool allow entry approves the call BEFORE can_use_tool is
        # consulted, and a settings-file allow rule can shadow the callback with
        # no warning at all. Listing it is how the permission host disappears.
        r.options(allowed_tools=["Read", "AskUserQuestion"])
    assert "AskUserQuestion" not in r.options().allowed_tools


def test_options_come_from_the_job_row_because_a_resume_only_has_the_row(
    con: sqlite3.Connection,
) -> None:
    r, _ = runner(con)
    fresh = r.options(resume=False)
    job = jobs.get(con, JOB)
    assert job is not None
    assert fresh.session_id == job.cc_session_id
    assert fresh.resume is None
    assert fresh.model == "claude-opus-5"
    assert fresh.effort == "xhigh"
    assert fresh.permission_mode == "plan"  # never dontAsk; it denies the whole feature
    assert fresh.cwd == job.cwd

    resumed = r.options(resume=True)
    assert resumed.resume == job.cc_session_id
    assert resumed.session_id is None


def test_all_three_hooks_are_wired_and_the_host_is_the_callback(con: sqlite3.Connection) -> None:
    r, _ = runner(con)
    options = r.options()
    assert set(options.hooks or {}) == {"PreToolUse", "Stop", "TaskCompleted"}
    assert options.can_use_tool is r.host


def test_an_effort_the_cli_does_not_know_is_refused_here_not_at_spawn(
    con: sqlite3.Connection,
) -> None:
    jobs.set_state(con, JOB, "queued", actor="test", effort="ludicrous")
    r, _ = runner(con)
    with pytest.raises(ValueError, match="effort"):
        r.options()


def test_a_job_with_no_session_id_cannot_be_started(con: sqlite3.Connection) -> None:
    con.execute("UPDATE jobs SET cc_session_id=NULL WHERE id=?", (JOB,))
    r, _ = runner(con)
    with pytest.raises(ValueError, match="resumed after a reboot"):
        r.options()


# ───────────────────────────── the state machine ─────────────────────────────


async def test_a_finished_run_walks_the_real_transitions(con: sqlite3.Connection) -> None:
    steps = [
        AssistantMessage(content=[TextBlock(text="Todos will be stored in SQLite.")], model="opus"),
        result_message(result="done", total_cost_usd=0.0348, usage={"input_tokens": 12}),
    ]
    r, made = runner(con, steps)
    outcome = await r.run("build me a todo cli")

    assert outcome.state == "done"
    assert outcome.text == ("Todos will be stored in SQLite.",)
    job = jobs.get(con, JOB)
    assert job is not None and job.state == "done"
    assert job.pid is not None  # attach_process ran in the process that owns it
    assert made[0].prompts == ["build me a todo cli"]
    assert made[0].closed


async def test_an_error_result_fails_the_job_rather_than_finishing_it(
    con: sqlite3.Connection,
) -> None:
    r, _ = runner(con, [result_message(is_error=True, stop_reason="error_max_turns")])
    outcome = await r.run("go")
    assert outcome.state == "failed"
    job = jobs.get(con, JOB)
    assert job is not None and job.state == "failed" and job.stop_reason == "error_max_turns"


async def test_a_crash_mid_stream_writes_down_why_before_it_propagates(
    con: sqlite3.Connection,
) -> None:
    def explode(_client: FakeClient) -> Any:
        raise RuntimeError("the CLI went away")

    r, _ = runner(con, [explode])
    with pytest.raises(RuntimeError, match="went away"):
        await r.run("go")
    job = jobs.get(con, JOB)
    # A runner that dies without saying why leaves a 'running' row that reconcile
    # can only call orphaned, and the user is told "I don't know".
    assert job is not None and job.state == "failed"
    assert "the CLI went away" in (job.stop_reason or "")


async def test_a_stream_that_ends_with_no_result_is_not_called_finished(
    con: sqlite3.Connection,
) -> None:
    r, _ = runner(con, [AssistantMessage(content=[TextBlock(text="hi")], model="opus")])
    with pytest.raises(RuntimeError, match="no result message"):
        await r.run("go")
    job = jobs.get(con, JOB)
    assert job is not None and job.state == "failed"


async def test_a_terminal_job_cannot_be_started_again(con: sqlite3.Connection) -> None:
    jobs.set_state(con, JOB, "killed", actor="test")
    r, _ = runner(con, [result_message()])
    with pytest.raises(jobs.IllegalTransition):
        await r.run("go")


# ───────────────────────────── the refusals ─────────────────────────────


async def test_a_settings_timeout_stops_the_runner_before_it_touches_the_job(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    bad = tmp_path / "managed-settings.json"
    bad.write_text('{"askUserQuestionTimeout": "60s"}', encoding="utf-8")
    made: list[FakeClient] = []
    r = drv.ClaudeJobRunner(
        con, JOB, client_factory=factory_for([result_message()], made), settings_paths=[str(bad)]
    )
    with pytest.raises(AskUserQuestionTimeoutSet):
        await r.run("go")

    job = jobs.get(con, JOB)
    assert job is not None and job.state == "queued"  # untouched
    assert made == []  # and no CLI was ever spawned


async def test_a_job_killed_while_it_was_queued_never_starts(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    kill.bump_epoch(other, actor="voice", reason="user said stop")
    r, made = runner(con, [result_message()])
    with pytest.raises(kill.KillEpochAdvanced):
        await r.run("go")
    assert made == []


# ───────────────────────── defer, death, answer, resume ─────────────────────────


async def test_the_defer_path_makes_the_question_durable_and_parks_the_job(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    deferred = DeferredToolUse(id="toolu_defer", name="AskUserQuestion", input=SINGLE)
    r, _ = runner(con, [result_message(stop_reason="tool_deferred", deferred_tool_use=deferred)])
    outcome = await r.run("build me a todo cli")

    assert outcome.deferred and outcome.state == "deferred"
    job = jobs.get(con, JOB)
    assert job is not None
    assert job.state == "deferred"
    assert job.blocked_request_id == outcome.deferred_request_id
    assert job.stop_reason == "tool_deferred"

    # Defer fires BEFORE can_use_tool, so the host never saw this call: the row
    # can only come from deferred_tool_use.input, and a SECOND connection — the
    # away channel — must be able to present it.
    row = rq.get_request(other, str(outcome.deferred_request_id))
    assert row is not None
    assert row.tool_use_id == "toolu_defer"
    assert row.payload == SINGLE
    assert [i["label"] for i in row.presentation["items"]] == ["SQLite", "JSON file"]


async def test_defer_then_answer_then_resume_across_two_runners(
    con: sqlite3.Connection, other: sqlite3.Connection, db_path: Path
) -> None:
    """Spike S1 scenario 4, with the CLI faked and everything else real."""
    deferred = DeferredToolUse(id="toolu_01G6XW", name="AskUserQuestion", input=SINGLE)
    parked_step = result_message(stop_reason="tool_deferred", deferred_tool_use=deferred)
    first, _ = runner(con, [parked_step])
    parked = await first.run("build me a todo cli")
    assert parked.deferred

    # The runner is gone. Some other process — Telegram, the phone, a voice
    # reply at the desk — answers a row that now outlives every process.
    answering = connect(db_path)
    try:
        req = rq.open_requests(answering, JOB)[0]
        assert rq.answer_request(answering, req.id, {"answers": {Q: "JSON file"}}, "phone", "dtmf")
    finally:
        answering.close()

    # A FRESH process resumes. The question re-fires with the SAME tool_use_id
    # (S1 measured that) and the stored answer satisfies it with nobody asked.
    resumed_con = connect(db_path)
    try:
        jobs.claim_resume(
            resumed_con,
            JOB,
            actor="resumer",
            expect_state="deferred",
            expect_resume_count=0,
        )
        second, made = runner(resumed_con, [asks(SINGLE, "toolu_01G6XW"), result_message()])
        outcome = await second.run(None, resume=True)
    finally:
        resumed_con.close()

    assert outcome.state == "done"
    allow = made[0].results[0]
    assert isinstance(allow, PermissionResultAllow)
    assert allow.updated_input == {**SINGLE, "answers": {Q: "JSON file"}}
    assert "response" not in (allow.updated_input or {})
    assert made[0].options.resume  # a resume, not a new session
    assert made[0].prompts == [drv.CONTINUE_PROMPT]

    job = jobs.get(other, JOB)
    assert job is not None and job.state == "done" and job.blocked_request_id is None


async def test_a_second_defer_of_the_same_question_does_not_duplicate_the_row(
    con: sqlite3.Connection,
) -> None:
    deferred = DeferredToolUse(id="toolu_same", name="AskUserQuestion", input=SINGLE)
    for _ in range(2):
        step = result_message(stop_reason="tool_deferred", deferred_tool_use=deferred)
        r, _ = runner(con, [step])
        await r.run("go")
        jobs.claim_resume(
            con,
            JOB,
            actor="resumer",
            expect_state="deferred",
            expect_resume_count=jobs.get(con, JOB).resume_count,  # type: ignore[union-attr]
        )
    # tool_use_id is stable across defer and resume, so it is the idempotency
    # key: asking twice must find the row, not raise on the unique index.
    assert len(rq.open_requests(con, JOB)) == 1


# ───────────────────────────── spend ─────────────────────────────


async def test_a_max_subscription_reports_no_dollars_and_that_is_not_an_error(
    con: sqlite3.Connection,
) -> None:
    r, _ = runner(
        con, [result_message(total_cost_usd=None, usage={"input_tokens": 900, "output_tokens": 30})]
    )
    outcome = await r.run("go")

    assert outcome.state == "done"
    assert outcome.total_cost_usd is None
    sql = "SELECT unit, amount, usd_equiv FROM spend WHERE job_id=?"
    rows = con.execute(sql, (JOB,)).fetchall()
    assert [(r["unit"], r["amount"]) for r in rows] == [("tokens", 930.0)]
    # Tokens are never dollars, and a subscription's dollars are never money.
    assert rows[0]["usd_equiv"] is None


async def test_reported_dollars_are_stored_but_stay_unpriced_under_a_subscription(
    con: sqlite3.Connection,
) -> None:
    r, _ = runner(con, [result_message(total_cost_usd=0.043, usage={"input_tokens": 10})])
    await r.run("go")
    rows = {
        row["unit"]: row["usd_equiv"]
        for row in con.execute("SELECT unit, usd_equiv FROM spend WHERE job_id=?", (JOB,))
    }
    assert rows["usd_est"] is None  # ADR 0004: an API-equivalent estimate, not a bill
    assert "tokens" in rows


async def test_nothing_measured_means_nothing_recorded_rather_than_a_zero(
    con: sqlite3.Connection,
) -> None:
    r, _ = runner(con, [result_message(total_cost_usd=None, usage=None)])
    await r.run("go")
    # A zero row would put "this build was free" into the spend table, which is
    # a lie with a number on it.
    assert con.execute("SELECT count(*) c FROM spend WHERE job_id=?", (JOB,)).fetchone()["c"] == 0


# ───────────────────────────── the entrypoint ─────────────────────────────


def test_the_entrypoint_refuses_loudly_and_says_so_in_its_exit_code(
    db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """No injected paths: the runner must FIND this file the way the CLI would.

    The settings file goes in the job's own working directory, which is where a
    project-level ``.claude/settings.json`` lives — so this exercises the real
    discovery order and not a list handed to it by the test.
    """
    from jarvis.cc import __main__ as entry

    project = tmp_path / ".claude"
    project.mkdir(exist_ok=True)
    (project / "settings.json").write_text('{"askUserQuestionTimeout": "5m"}', encoding="utf-8")

    code = entry.main(["--job-id", JOB, "--db", str(db_path)])
    assert code == entry.EXIT_REFUSED
    assert "askUserQuestionTimeout" in capsys.readouterr().out
    # And it refused before spawning anything: the job never left the queue.
    checker = connect(db_path)
    try:
        job = jobs.get(checker, JOB)
        assert job is not None and job.state == "queued"
    finally:
        checker.close()


def test_the_entrypoint_reports_an_unknown_job_without_a_traceback(
    db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from jarvis.cc import __main__ as entry

    code = entry.main(["--job-id", "job_nosuchjobxx", "--db", str(db_path)])
    assert code == entry.EXIT_NO_JOB
    assert "no job" in capsys.readouterr().out


def test_the_entrypoint_never_dies_because_stdout_went_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis.cc import __main__ as entry

    class ClosedPipe:
        def write(self, _text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    # It is launched detached by systemd-run: there may be no stdout at all, and
    # a successful build must not become a traceback because of it.
    monkeypatch.setattr("sys.stdout", ClosedPipe())
    entry._say({"job_id": JOB, "state": "done"})
