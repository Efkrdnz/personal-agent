"""The hop, tested where it can actually lie: fidelity, idempotency and wording.

``code_build`` writes one row and says one sentence. The row is the contract
with a process that does not exist yet, so the tests assert the COLUMNS; the
sentence is heard by a human who will act on it, so the tests assert what it must
not claim.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs
from jarvis.bus import read_since
from jarvis.db import connect, migrate
from jarvis.tools.builtin import code_build, status
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.default import registry
from jarvis.tools.registry import Registry

SPOKEN = (
    "let's build an app that watches my YouTube comments and pings me when somebody "
    "asks a question, use Opus with max effort, and no Docker"
)


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def ctx_for(con: sqlite3.Connection, transcript: str | None = SPOKEN, **extra: object) -> ToolCtx:
    payload: dict[str, object] = dict(extra)
    if transcript is not None:
        payload[code_build.TRANSCRIPT] = transcript
    return ToolCtx(con=con, channel="desk", actor="desk", extra=payload)


def only_job(con: sqlite3.Connection) -> jobs.Job:
    rows = con.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == 1, f"expected one job, found {len(rows)}"
    return jobs.to_job(rows[0])


# ───────────────────────────── the row ─────────────────────────────


def test_one_sentence_becomes_one_queued_job(con: sqlite3.Connection) -> None:
    code_build.code_build(ctx_for(con), project_name="comment watcher")
    job = only_job(con)
    assert job.kind == code_build.BUILD_JOB_KIND
    assert job.state == "queued"
    assert job.title == "comment watcher"
    assert job.created_by == "desk"


def test_the_row_carries_the_users_words_not_the_models(con: sqlite3.Connection) -> None:
    """The whole fidelity chain reads prompt_text. It must be the transcript."""
    code_build.code_build(
        ctx_for(con),
        project_name="comment watcher",
        summary="a YouTube notification bot with sensible defaults",
    )
    job = only_job(con)
    assert job.prompt_text == SPOKEN
    assert "sensible defaults" not in (job.prompt_text or "")


def test_model_and_effort_come_from_the_transcript_not_the_call(con: sqlite3.Connection) -> None:
    code_build.code_build(ctx_for(con))
    job = only_job(con)
    assert job.model == "opus"
    assert job.effort == "max"


def test_no_repository_is_created_and_no_claude_code_job_appears(con: sqlite3.Connection) -> None:
    """The stage's whole order claim: the repo comes later, after a yes."""
    code_build.code_build(ctx_for(con))
    kinds = {r["kind"] for r in con.execute("SELECT kind FROM jobs")}
    assert kinds == {code_build.BUILD_JOB_KIND}
    assert con.execute("SELECT COUNT(*) c FROM effects").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 0


def test_the_request_is_in_the_activity_log(con: sqlite3.Connection) -> None:
    code_build.code_build(ctx_for(con), project_name="comment watcher")
    kinds = [e.kind for e in read_since(con, "t")]
    assert "job.created" in kinds
    assert "project.requested" in kinds


# ───────────────────────────── refusals ─────────────────────────────


def test_no_transcript_is_a_refusal_not_a_fallback_to_the_paraphrase(
    con: sqlite3.Connection,
) -> None:
    with pytest.raises(code_build.NoTranscript):
        code_build.code_build(ctx_for(con, transcript=None), summary="build me a thing")
    assert con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_a_blank_transcript_is_the_same_refusal(con: sqlite3.Connection) -> None:
    with pytest.raises(code_build.NoTranscript):
        code_build.code_build(ctx_for(con, transcript="   \n "))


def test_a_second_call_while_one_is_in_flight_is_refused(con: sqlite3.Connection) -> None:
    code_build.code_build(ctx_for(con), project_name="comment watcher")
    with pytest.raises(code_build.AlreadyBuilding, match="comment watcher"):
        code_build.code_build(ctx_for(con), project_name="something else")
    assert con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1


def test_a_finished_build_does_not_block_the_next_one(con: sqlite3.Connection) -> None:
    code_build.code_build(ctx_for(con), project_name="first")
    job = only_job(con)
    jobs.set_state(con, job.id, "starting", actor="t")
    jobs.set_state(con, job.id, "running", actor="t")
    jobs.set_state(con, job.id, "finishing", actor="t")
    jobs.set_state(con, job.id, "done", actor="t")
    code_build.code_build(ctx_for(con), project_name="second")
    assert con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 2


# ───────────────────────────── what it says ─────────────────────────────


def test_the_acknowledgement_never_claims_the_repo_exists(con: sqlite3.Connection) -> None:
    said = code_build.code_build(ctx_for(con), project_name="comment watcher")
    lowered = said.lower()
    # Past tense is the tell. Nothing has happened yet except a row.
    for lie in ("i've created", "i created", "i've made", "pushed", "cloned", "is ready"):
        assert lie not in lowered, f"acknowledgement claims too much: {said!r}"
    assert "read the requirements back" in lowered
    assert "before anything is created" in lowered


def test_high_effort_is_called_out_as_a_no_op(con: sqlite3.Connection) -> None:
    said = code_build.code_build(
        ctx_for(con, transcript="build me a todo app with Opus 5 at high effort")
    )
    assert "already the default" in said.lower()


def test_an_unknown_effort_level_is_admitted(con: sqlite3.Connection) -> None:
    said = code_build.code_build(
        ctx_for(con, transcript="build me a todo app with aggressive effort")
    )
    assert "aggressive" in said
    assert "default" in said.lower()


def test_asking_for_the_cloud_is_answered_out_loud(con: sqlite3.Connection) -> None:
    said = code_build.code_build(
        ctx_for(con, transcript="build me a todo app in the cloud, nothing on my disk")
    )
    assert "locally" in said.lower()
    modes = [e for e in read_since(con, "t") if e.kind == "project.mode_asked"]
    assert modes and modes[0].payload["asked"] == "cloud"


def test_an_unnamed_build_still_gets_a_sayable_title(con: sqlite3.Connection) -> None:
    code_build.code_build(ctx_for(con, transcript="make me something that renames my files"))
    assert "renames my files" in only_job(con).title


# ───────────────────────────── the channel column ─────────────────────────────


def test_the_phone_cannot_start_a_build(con: sqlite3.Connection) -> None:
    r = registry()
    assert "code_build" not in r.names("phone")
    said = r.dispatch("code_build", {}, ToolCtx(con=con, channel="phone", actor="phone"))
    assert "phone" in said
    assert con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_dispatch_through_the_registry_reaches_the_handler(con: sqlite3.Connection) -> None:
    r = registry()
    said = r.dispatch("code_build", {"project_name": "comment watcher"}, ctx_for(con))
    assert "comment watcher" in said
    assert only_job(con).title == "comment watcher"


def test_a_refusal_reaches_the_user_as_a_sentence(con: sqlite3.Connection) -> None:
    r = registry()
    said = r.dispatch("code_build", {}, ctx_for(con, transcript=None))
    assert "say it again" in said.lower()


# ───────────────────────────── status ─────────────────────────────


def test_status_names_the_window_when_there_is_nothing_to_say(con: sqlite3.Connection) -> None:
    said = status.project_status(ctx_for(con))
    assert "24 hours" in said


def test_status_reports_what_is_running(con: sqlite3.Connection) -> None:
    job = jobs.create_job(con, kind="claude_code", title="the todo app build", created_by="t")
    jobs.set_state(con, job.id, "starting", actor="t")
    jobs.set_state(con, job.id, "running", actor="t")
    said = status.project_status(ctx_for(con))
    assert "the todo app build" in said


def test_spend_names_the_unpriced_meters(con: sqlite3.Connection) -> None:
    said = status.spend(ctx_for(con), window="today")
    assert said
    bad = status.spend(ctx_for(con), window="since tuesday")
    assert "don't know the window" in bad


def test_spend_uses_the_threshold_it_was_handed(con: sqlite3.Connection) -> None:
    from jarvis import ledger

    ledger.record(con, "gemini", "seconds", 10.0, usd_equiv=5.0)
    said = status.spend(ctx_for(con, spend_threshold_usd=10.0), window="today")
    assert "10" in said


def test_reachability_repeats_the_evidence_verbatim(con: sqlite3.Connection) -> None:
    said = status.reachability(ctx_for(con))
    assert "reach you on" in said


def test_every_shipped_tool_is_registrable() -> None:
    r = Registry()
    from jarvis.tools.default import BUILTIN

    for tool in BUILTIN:
        r.add(tool)
        assert tool.description.strip(), f"{tool.name} has no description for the model to read"
        assert tool.parameters.get("type") == "OBJECT"
    assert len(r) == len(BUILTIN)
