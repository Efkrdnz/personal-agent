"""`run`, `pending`, `answer` — the front door the driver never had.

Until these existed, the only caller of ``create_job(kind='claude_code')``
outside tests was a spike script, so "drive Claude Code" meant "write Python".

The tests that matter here are the ones about what happens when the child does
NOT behave: a spawn that fails, a question raised on the way out, a job parked on
an answer. The happy path is verified live against the real CLI; these pin the
edges that a live run would not reach.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import __main__ as cli
from jarvis import jobs, kill
from jarvis import requests as rq
from jarvis.cc import gate
from jarvis.db import connect, migrate


@pytest.fixture
def dbpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    c.close()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    yield p


QUESTIONS = {
    "questions": [
        {
            "question": "How should todos be stored?",
            "options": [{"label": "SQLite"}, {"label": "JSON file"}],
        }
    ]
}


class FakeChild:
    """A driver that exits immediately, optionally writing a request first."""

    def __init__(self, dbpath: Path | None = None, *, job_id: str = "", rc: int = 0) -> None:
        self._dbpath, self._job_id, self.returncode = dbpath, job_id, rc
        self._polls = 0

    def poll(self) -> int | None:
        self._polls += 1
        if self._polls < 2:
            return None
        if self._dbpath is not None:
            # Raised on the way out, on its own connection — the defer path.
            con = connect(self._dbpath)
            try:
                gate.ensure_request(
                    con,
                    tool_name="AskUserQuestion",
                    input_data=QUESTIONS,
                    job_id=self._job_id,
                    tool_use_id="toolu_late",
                    actor="runner",
                )
            finally:
                con.close()
            self._dbpath = None
        return self.returncode


def run_cli(argv: list[str], dbpath: Path) -> int:
    return cli.main(["--db", str(dbpath), *argv])


def only_job(dbpath: Path) -> jobs.Job:
    con = connect(dbpath)
    try:
        return jobs.to_job(con.execute("SELECT * FROM jobs").fetchone())
    finally:
        con.close()


# ───────────────────────────── run ─────────────────────────────


def test_run_stamps_the_job_with_the_current_kill_epoch(
    dbpath: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the job is born doomed on any machine that has ever killed.

    ``create_job`` hardcodes ``kill_epoch: 0``, and ``ClaudeJobRunner.run`` calls
    ``kill.assert_epoch`` before anything else — so a job created at 0 after one
    kill refuses to start, forever, with a message about a kill the user does not
    remember.
    """
    con = connect(dbpath)
    try:
        kill.bump_epoch(con, actor="user", reason="stop everything")
        epoch = kill.current_epoch(con)
    finally:
        con.close()
    assert epoch > 0

    monkeypatch.setattr(cli, "_spawn_runner", lambda *a, **k: FakeChild())
    run_cli(["run", "build a thing", "--into", str(tmp_path)], dbpath)
    assert only_job(dbpath).kill_epoch == epoch


def test_run_refuses_without_the_sdk_and_creates_nothing(
    dbpath: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_installed", lambda m: m != "claude_agent_sdk")
    assert run_cli(["run", "build a thing", "--into", str(tmp_path)], dbpath) == 2
    assert "pip install -e '.[cc]'" in capsys.readouterr().err
    con = connect(dbpath)
    try:
        assert con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
    finally:
        con.close()


def test_a_failed_spawn_does_not_leave_a_job_nothing_can_ever_see(
    dbpath: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reconcile`` only scans ACTIVE_STATES, and 'queued' is not one.

    A row left queued by a dead spawn is invisible to every process in the system
    forever — it is not resumed, not reported, not cleaned up.
    """

    def boom(*a: object, **k: object) -> None:
        raise OSError("no such executable")

    monkeypatch.setattr(cli, "_spawn_runner", boom)
    assert run_cli(["run", "build a thing", "--into", str(tmp_path)], dbpath) == 2
    job = only_job(dbpath)
    assert job.state == "failed"
    assert "spawn failed" in (job.stop_reason or "")


def test_run_announces_a_question_the_child_raised_on_its_way_out(
    dbpath: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not a corner case — it is the whole defer path.

    ``hooks.pre_tool`` writes the row and asks for a defer, and the process exits
    at once. A watcher that only swept before each poll would never print the one
    question the user has to answer to get their build back.
    """
    made: dict[str, FakeChild] = {}

    def spawn(job_id: str, db: str | None, **k: object) -> FakeChild:
        made["c"] = FakeChild(dbpath, job_id=job_id)
        return made["c"]

    monkeypatch.setattr(cli, "_spawn_runner", spawn)
    monkeypatch.setattr(cli, "RUN_POLL_S", 0.0)
    run_cli(["run", "build a thing", "--into", str(tmp_path)], dbpath)
    out = capsys.readouterr().out
    assert "How should todos be stored?" in out
    assert "1. SQLite" in out


def test_run_refuses_a_directory_that_is_not_one(dbpath: Path, tmp_path: Path) -> None:
    assert run_cli(["run", "x", "--into", str(tmp_path / "nope")], dbpath) == 2


def test_run_without_a_prompt_says_what_to_type(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["run"], dbpath) == 1
    assert "what should I build" in capsys.readouterr().err


def test_the_child_argv_is_one_jarvis_cc_actually_accepts(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flags are strings on both sides of a process boundary and nothing checks them.

    Parsed with the REAL parser, so a renamed flag fails here rather than as an
    exit-2 from a subprocess whose stderr nobody is reading.
    """
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **k: captured.__setitem__("argv", argv) or FakeChild()
    )
    cli._spawn_runner("job_1", str(dbpath), prompt="hello")
    argv = captured["argv"]
    assert argv[1:3] == ["-m", "jarvis.cc"]

    from jarvis.cc.__main__ import build_parser

    parsed = build_parser().parse_args(argv[3:])
    assert parsed.job_id == "job_1" and parsed.prompt == "hello" and parsed.channel == "cli"

    cli._spawn_runner("job_1", str(dbpath), resume=True)
    assert build_parser().parse_args(captured["argv"][3:]).resume is True


def test_the_child_keeps_the_environment_it_needs(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare env dict strips PATH and HOME, and the bundled CLI will not start."""
    captured: dict[str, dict] = {}
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: captured.update(k) or FakeChild())
    monkeypatch.setenv("PATH", "/usr/bin")
    cli._spawn_runner("job_1", str(dbpath), prompt="x")
    assert captured["env"]["PATH"] == "/usr/bin"
    assert captured["env"]["JARVIS_DB"] == str(dbpath)


# ───────────────────────────── pending and answer ─────────────────────────────


def a_question(dbpath: Path, job_id: str | None = None) -> rq.Request:
    con = connect(dbpath)
    try:
        return gate.ensure_request(
            con,
            tool_name="AskUserQuestion",
            input_data=QUESTIONS,
            job_id=job_id,
            tool_use_id=f"toolu_{job_id or 'x'}",
            actor="runner",
        )
    finally:
        con.close()


def test_pending_numbers_the_options_the_way_every_channel_does(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_question(dbpath)
    assert run_cli(["pending"], dbpath) == 0
    out = capsys.readouterr().out
    assert "1. SQLite" in out and "2. JSON file" in out


def test_pending_on_a_quiet_system_says_so(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["pending"], dbpath) == 0
    assert "nothing is waiting" in capsys.readouterr().out


def test_answer_resolves_the_index_to_the_exact_label(dbpath: Path) -> None:
    """The model — and the human — emit an INDEX. The label is looked up by code."""
    req = a_question(dbpath)
    assert run_cli(["answer", "1", "1"], dbpath) == 0
    con = connect(dbpath)
    try:
        fresh = rq.get_request(con, req.id)
    finally:
        con.close()
    assert fresh is not None and fresh.state == "answered"
    assert fresh.answer == {
        "answers": {"How should todos be stored?": "SQLite"},
        "sources": {"How should todos be stored?": "option"},
    }
    assert fresh.answer_mode == "hud"


def test_answer_takes_free_text_as_the_users_own_words(dbpath: Path) -> None:
    a_question(dbpath)
    assert run_cli(["answer", "1", "--text", "put them in Postgres"], dbpath) == 0
    con = connect(dbpath)
    try:
        answered = con.execute("SELECT answer FROM requests").fetchone()["answer"]
    finally:
        con.close()
    assert "put them in Postgres" in answered
    assert "Other" not in answered


def test_answer_refuses_an_option_that_does_not_exist(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_question(dbpath)
    assert run_cli(["answer", "1", "9"], dbpath) == 1
    assert capsys.readouterr().err.strip()


def test_answer_refuses_a_question_number_that_does_not_exist(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["answer", "3", "1"], dbpath) == 1
    assert "pending" in capsys.readouterr().err


def test_answering_a_parked_job_names_the_command_that_resumes_it(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The driver EXITED when it deferred. Without this the user answers and waits forever."""
    con = connect(dbpath)
    try:
        job = jobs.create_job(con, kind="claude_code", title="t", created_by="cli")
        jobs.set_state(con, job.id, "starting", actor="t")
        jobs.set_state(con, job.id, "running", actor="t")
    finally:
        con.close()
    req = a_question(dbpath, job.id)
    con = connect(dbpath)
    try:
        jobs.mark_blocked(con, job.id, req.id, actor="t", state="deferred")
    finally:
        con.close()

    assert run_cli(["answer", "1", "1"], dbpath) == 0
    assert "python -m jarvis run --resume" in capsys.readouterr().out


def test_a_second_answer_loses_rather_than_overwriting(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_question(dbpath)
    run_cli(["answer", "1", "1"], dbpath)
    capsys.readouterr()
    assert run_cli(["answer", "1", "2"], dbpath) == 1
    assert "pending" in capsys.readouterr().err


def test_resume_with_nothing_parked_says_so(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["run", "--resume"], dbpath) == 0
    assert "nothing to resume" in capsys.readouterr().out


# ───────────────────── answering the question you actually read ─────────────────────


def test_answering_by_id_is_immune_to_the_list_shifting(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A position is resolved AFTER the user types it, and the list moves.

    With two builds running, `answer 1 2` read one question and settled its
    neighbour — an `Allow` landing on a tool permission nobody had read.
    """
    first = a_question(dbpath)
    con = connect(dbpath)
    try:
        second = gate.ensure_request(
            con,
            tool_name="AskUserQuestion",
            input_data={
                "questions": [
                    {
                        "question": "Run rm -rf build?",
                        "options": [{"label": "Allow"}, {"label": "Deny"}],
                    }
                ]
            },
            job_id=None,
            tool_use_id="toolu_second",
            actor="runner",
        )
        # The user read `pending`, then something answered the FIRST one elsewhere.
        rq.answer_request(
            con,
            first.id,
            {"answers": {"How should todos be stored?": "SQLite"}},
            "telegram:1",
            "button",
        )
    finally:
        con.close()

    # The id they were shown still means what it meant.
    assert run_cli(["answer", second.id, "2"], dbpath) == 0
    con = connect(dbpath)
    try:
        assert rq.get_request(con, second.id).answer["answers"] == {"Run rm -rf build?": "Deny"}
    finally:
        con.close()


def test_pending_prints_the_id_the_answer_command_takes(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    req = a_question(dbpath)
    run_cli(["pending"], dbpath)
    out = capsys.readouterr().out
    assert req.id in out
    assert f"python -m jarvis answer {req.id}" in out


def test_a_stale_position_refuses_rather_than_settling_its_neighbour(
    dbpath: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_question(dbpath)
    assert run_cli(["answer", "4", "1"], dbpath) == 1
    assert "no open question" in capsys.readouterr().err


# ───────────────────── the child's own words ─────────────────────


def test_the_child_inherits_the_terminal_rather_than_a_pipe_nobody_reads(
    dbpath: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PIPE nobody drains is two bugs: a discarded refusal, and an eventual deadlock.

    `_watch` polls the database and never reads the child's output, so its exit-2
    refusal ("a settings file would auto-close a pending question") went nowhere,
    and a chatty child would block forever on a full pipe buffer.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: captured.update(k) or FakeChild())
    cli._spawn_runner("job_1", str(dbpath), prompt="x")
    assert "stdout" not in captured and "stderr" not in captured


def test_a_child_that_refuses_at_startup_leaves_a_visible_row(
    dbpath: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`queued` is scanned by nothing: reconcile looks at ACTIVE_STATES and deferred/orphaned."""
    monkeypatch.setattr(cli, "_spawn_runner", lambda *a, **k: FakeChild(rc=2))
    monkeypatch.setattr(cli, "RUN_POLL_S", 0.0)
    assert run_cli(["run", "build a thing", "--into", str(tmp_path)], dbpath) == 1
    job = only_job(dbpath)
    assert job.state == "parked", "a queued row is invisible to every process in the system"
    assert "exited 2" in (job.stop_reason or "")
