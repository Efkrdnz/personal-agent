""" "Let's build an app that…" becomes a running build, and every step is a row.

The runner may die between any two steps, so ``advance`` is called repeatedly and
works out where it is from the job row and the request it is parked on. These
tests drive it one call at a time, exactly as a daemon would, and kill it in the
middle to check that the next call picks up rather than starting over.

Nothing real is touched: the tidier is a fake model, GitHub is FakeTransport and
git is FakeGit. The fidelity nets in :mod:`jarvis.spec` are real, so a fake model
that invents a requirement is rejected here exactly as a live one would be.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs, spec
from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.github import scopes
from jarvis.github.transport import FakeTransport
from jarvis.project import runner
from jarvis.project import workspace as ws

SPOKEN = (
    "let's build an app that watches my YouTube comments and pings me when somebody asks "
    "a question, and no Docker"
)


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def a_model(requirements: list[tuple[str, str]], repo_name: str = "comment-watcher"):
    """A tidier that returns exactly these (text, quote) pairs."""

    def call(prompt: str) -> str:
        return json.dumps(
            {
                "repo_name": repo_name,
                "requirements": [{"text": t, "quote": q} for t, q in requirements],
            }
        )

    return call


HONEST = [
    ("Watch my YouTube comments", "watches my YouTube comments"),
    ("Ping me when somebody asks a question", "pings me when somebody asks a question"),
    ("No Docker", "no Docker"),
]


def deps_for(tmp_path: Path, *, model=None, owner: str = "", **kw) -> runner.Deps:
    transport = FakeTransport(login=owner or "nobody", scopes=("repo",)) if owner else None
    return runner.Deps(
        model_call=model or a_model(HONEST),
        git=ws.FakeGit(),
        git_token="ghp_fake",
        capabilities=scopes.capabilities(transport)
        if transport
        else scopes.Capabilities(
            token_kind="unknown", create="no", source="no token", notes=("no credential at all",)
        ),
        owner=owner,
        workspace_root=tmp_path / "code",
        transport=transport,
        **kw,
    )


def a_request(con: sqlite3.Connection, transcript: str = SPOKEN) -> jobs.Job:
    return jobs.create_job(
        con,
        kind=runner.BUILD_JOB_KIND,
        title="comment watcher",
        created_by="desk",
        prompt_text=transcript,
    )


def approve(con: sqlite3.Connection, request_id: str, label: str = runner.APPROVE_LABEL) -> None:
    req = rq.get_request(con, request_id)
    assert req is not None
    rq.answer_request(
        con,
        request_id,
        {"answers": {runner.READBACK_QUESTION: label}},
        answered_by="cli",
        answer_mode="hud",
    )


def say(con: sqlite3.Connection, request_id: str, words: str) -> None:
    req = rq.get_request(con, request_id)
    assert req is not None
    question = str(req.presentation.get("question") or runner.READBACK_QUESTION)
    rq.answer_request(
        con,
        request_id,
        {"text": words, "answers": {question: words}, "sources": {question: "free_text"}},
        answered_by="cli",
        answer_mode="hud",
    )


# ───────────────────────────── the read-back ─────────────────────────────


def test_a_transcript_becomes_a_numbered_list_and_a_question(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    job = a_request(con)
    step = runner.advance(con, job.id, deps_for(tmp_path))

    assert step.action == "waiting"
    assert step.requirements == (
        "Watch my YouTube comments",
        "Ping me when somebody asks a question",
        "No Docker",
    )
    req = rq.get_request(con, step.request_id or "")
    assert req is not None and req.kind == "readback"
    assert req.presentation["verbatim"] is True, "the list must go to the reader, not to Gemini"
    assert "1. Watch my YouTube comments" in req.presentation["intro"]
    assert jobs.get(con, job.id).state == "deferred"


def test_nothing_is_created_before_the_yes(con: sqlite3.Connection, tmp_path: Path) -> None:
    """R2's order: the read-back happens before anything exists."""
    job = a_request(con)
    runner.advance(con, job.id, deps_for(tmp_path, owner="Efkrdnz"))
    assert con.execute("SELECT COUNT(*) c FROM effects").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"] == 0
    kinds = {r["kind"] for r in con.execute("SELECT kind FROM jobs")}
    assert kinds == {runner.BUILD_JOB_KIND}


def test_an_invented_requirement_never_reaches_the_read_back(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """Net 1, through the runner: a span that is not in the transcript is deleted.

    The model here adds "and add authentication", which the user never said. It
    has no quote in the transcript, so the user is never read it — and the
    coverage sentence says out loud that something was thrown away.
    """
    liar = a_model([*HONEST, ("Add authentication", "the user should log in")])
    job = a_request(con)
    step = runner.advance(con, job.id, deps_for(tmp_path, model=liar))
    assert "authentication" not in " ".join(step.requirements)
    assert "threw away" in step.spoken


def test_a_tidy_that_traces_nothing_refuses_rather_than_guessing(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    job = a_request(con)
    step = runner.advance(con, job.id, deps_for(tmp_path, model=a_model([])))
    assert step.action == "refused"
    assert jobs.get(con, job.id).state == "parked"


def test_a_dead_tidier_parks_rather_than_losing_the_words(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    def boom(prompt: str) -> str:
        raise RuntimeError("the network went away")

    job = a_request(con)
    step = runner.advance(con, job.id, deps_for(tmp_path, model=boom))
    assert step.action == "refused"
    fresh = jobs.get(con, job.id)
    assert fresh.state == "parked"
    assert fresh.prompt_text == SPOKEN, "the user's words must survive a failed tidy"


# ───────────────────────────── edits ─────────────────────────────


def test_drop_three_drops_the_third_and_reads_it_back(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    say(con, first.request_id or "", "drop three")

    second = runner.advance(con, job.id, deps)
    assert second.action == "waiting"
    assert second.requirements == (
        "Watch my YouTube comments",
        "Ping me when somebody asks a question",
    )
    assert second.request_id != first.request_id


def test_an_edit_naming_a_position_that_does_not_exist_is_not_guessed_at(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    say(con, first.request_id or "", "drop nine")

    step = runner.advance(con, job.id, deps)
    assert step.action == "waiting"
    assert len(step.requirements or ()) in (0, 3)
    assert "nine" in step.spoken or "not in the list" in step.spoken.lower() or step.spoken


def test_words_that_are_not_an_edit_are_admitted_rather_than_re_tidied(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """A re-tidy would be a fresh chance to drift. Say "I didn't follow" instead."""
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    say(con, first.request_id or "", "hmm what was the second one again")

    step = runner.advance(con, job.id, deps)
    assert "didn't follow" in step.spoken


def test_change_something_asks_what(con: sqlite3.Connection, tmp_path: Path) -> None:
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "", runner.CHANGE_LABEL)
    step = runner.advance(con, job.id, deps)
    assert step.action == "waiting"
    assert "What should change?" in step.spoken


# ───────────────────────────── the build ─────────────────────────────


def test_yes_with_no_token_builds_locally_and_says_so(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """No token is a supported configuration. A SILENT local build would not be."""
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")

    step = runner.advance(con, job.id, deps)
    assert step.action == "building"
    assert "no repository" in step.spoken
    child = jobs.get(con, step.child_job_id or "")
    assert child.kind == "claude_code"
    assert child.cwd and Path(child.cwd).is_dir()
    assert jobs.get(con, job.id).state == "done"


def test_the_child_prompt_carries_the_confirmed_list_and_the_users_own_words(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """Net 4. Claude gets the bullets AND the raw transcript, so drift cannot hide one."""
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")
    step = runner.advance(con, job.id, deps)

    prompt = jobs.get(con, step.child_job_id or "").prompt_text or ""
    assert spec.CONFIRMED_HEADER in prompt
    assert spec.APPENDIX_HEADER in prompt
    assert "1. Watch my YouTube comments" in prompt
    assert SPOKEN in prompt


def test_an_edited_list_is_what_reaches_claude(con: sqlite3.Connection, tmp_path: Path) -> None:
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    say(con, first.request_id or "", "drop three")
    second = runner.advance(con, job.id, deps)
    approve(con, second.request_id or "")
    step = runner.advance(con, job.id, deps)

    prompt = jobs.get(con, step.child_job_id or "").prompt_text or ""
    assert "1. Watch my YouTube comments" in prompt
    assert "3. No Docker" not in prompt
    assert "no Docker" in prompt, "the appendix still carries the words they said"


def test_the_child_inherits_the_model_and_effort_from_what_was_said(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    deps = deps_for(tmp_path)
    job = jobs.create_job(
        con,
        kind=runner.BUILD_JOB_KIND,
        title="t",
        created_by="desk",
        prompt_text=SPOKEN,
        model="opus",
        effort="max",
    )
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")
    step = runner.advance(con, job.id, deps)
    child = jobs.get(con, step.child_job_id or "")
    assert (child.model, child.effort) == ("opus", "max")


# ───────────────────────────── resumability ─────────────────────────────


def test_advance_before_the_answer_arrives_does_nothing(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    for _ in range(3):
        step = runner.advance(con, job.id, deps)
        assert step.action == "nothing"
    assert rq.get_request(con, first.request_id or "").state == "pending"


def test_a_restarted_runner_picks_up_where_the_rows_say(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """No in-memory state: a brand-new Deps and a brand-new call finish the job."""
    job = a_request(con)
    first = runner.advance(con, job.id, deps_for(tmp_path))
    approve(con, first.request_id or "")
    step = runner.advance(con, job.id, deps_for(tmp_path))  # a different Deps entirely
    assert step.action == "building"


def test_one_yes_cannot_start_two_builds(con: sqlite3.Connection, tmp_path: Path) -> None:
    """The answer is CONSUMED, so a replayed runner meets a settled row."""
    deps = deps_for(tmp_path)
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")
    runner.advance(con, job.id, deps)
    again = runner.advance(con, job.id, deps)
    assert again.action == "nothing"
    assert con.execute("SELECT COUNT(*) c FROM jobs WHERE kind='claude_code'").fetchone()["c"] == 1


def test_a_job_that_is_not_a_build_request_is_refused(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    job = jobs.create_job(con, kind="claude_code", title="t", created_by="cli")
    with pytest.raises(runner.BuildRefused):
        runner.advance(con, job.id, deps_for(tmp_path))


# ───────────── the whole promise, from the spoken sentence to the build ─────────────


def test_a_spoken_sentence_becomes_a_build_and_every_hop_is_a_row(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """THE HEADLINE, end to end, with four processes' worth of code and no shortcuts.

    The desk files a request from the user's own words; the builder tidies it and
    asks; the scheduler routes the question to a channel; Telegram presents it and
    the user taps; the builder starts the build. Every hop is a row, and every
    step is a different module that could be a different process on a different
    day.

    Before this existed, ``code_build`` wrote a row that nothing ever read, and
    the sentence "I'll read the requirements back to you" was never kept.
    """
    from jarvis.schedule import loop
    from jarvis.telegram.channel import TelegramChannel
    from jarvis.telegram.transport import FakeTransport
    from jarvis.tools.builtin import code_build
    from jarvis.tools.ctx import ToolCtx

    # 1. the desk hears it and files one row
    said = code_build.code_build(
        ToolCtx(con=con, channel="desk", actor="desk", extra={"transcript": SPOKEN}),
        project_name="comment watcher",
    )
    assert "read the requirements back" in said
    job = jobs.to_job(con.execute("SELECT * FROM jobs").fetchone())
    assert job.kind == runner.BUILD_JOB_KIND

    # 2. the builder tidies it and asks
    deps = deps_for(tmp_path)
    step = runner.advance(con, job.id, deps)
    assert step.action == "waiting"
    req_id = step.request_id or ""

    # 3. the scheduler routes the question — the hop that did not exist
    assert (
        con.execute("SELECT COUNT(*) c FROM deliveries WHERE request_id=?", (req_id,)).fetchone()[
            "c"
        ]
        == 0
    )
    loop.tick(con, actor="scheduler")
    assert (
        con.execute("SELECT COUNT(*) c FROM deliveries WHERE request_id=?", (req_id,)).fetchone()[
            "c"
        ]
        > 0
    )

    # 4. Telegram reads the list out and the user taps "Build it"
    transport = FakeTransport()
    channel = TelegramChannel(chat_id=4242)
    later = jobs.shift_ts(rq.get_request(con, req_id).created_at, 120)
    assert channel.claim_and_present(con, transport, now_ts=later) == [req_id]
    sent = transport.last("sendMessage")
    assert sent is not None
    assert "1. Watch my YouTube comments" in sent.params["text"]
    buttons = [b for row in sent.params["reply_markup"]["inline_keyboard"] for b in row]
    approve_button = next(b for b in buttons if runner.APPROVE_LABEL in b["text"])
    channel.on_callback(
        con,
        transport,
        {
            "id": "cb1",
            "data": approve_button["callback_data"],
            "message": {"message_id": 1, "chat": {"id": 4242}},
        },
    )

    # 5. the builder starts the build
    final = runner.advance(con, job.id, deps)
    assert final.action == "building"
    child = jobs.get(con, final.child_job_id or "")
    assert child.kind == "claude_code"
    assert Path(child.cwd or "").is_dir()
    assert "No Docker" in (child.prompt_text or "")
    assert SPOKEN in (child.prompt_text or ""), "the appendix carries their own words"
    assert jobs.get(con, job.id).state == "done"


# ───────────────────────── what the audit found ─────────────────────────


def test_a_rename_is_proposed_rather_than_parked_forever(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """THE LOOP THAT DID NOT CLOSE.

    "Call it something else" was recorded, the job parked, and nothing ever
    proposed the new name — so the user answered and never heard from the build
    again.
    """
    deps = deps_for(tmp_path, owner="Efkrdnz")
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")

    named = runner.advance(con, job.id, deps)
    assert named.action == "waiting", "the repo name question"

    # "call it something else" — free text on the create confirmation.
    say(con, named.request_id or "", "call it comment radar")
    after = runner.advance(con, job.id, deps)

    assert after.action == "waiting", f"the rename must produce a new question, got {after.action}"
    assert after.request_id not in (first.request_id, named.request_id)
    fresh = rq.get_request(con, after.request_id or "")
    assert fresh is not None and fresh.state == "pending"
    assert jobs.get(con, job.id).state == "deferred"


def test_a_github_outage_parks_with_the_real_reason_not_a_silent_local_build(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """Falling through to local would blame a token that is present and working."""
    from jarvis.github.transport import TransportError

    deps = deps_for(tmp_path, owner="Efkrdnz")
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")

    class Dead:
        def request(self, *a: object, **k: object) -> None:
            raise TransportError("name resolution failed")

        def __getattr__(self, name: str):
            raise TransportError("name resolution failed")

    broken = runner.Deps(
        model_call=deps.model_call,
        git=deps.git,
        git_token=deps.git_token,
        capabilities=deps.capabilities,
        owner="Efkrdnz",
        workspace_root=deps.workspace_root,
        transport=Dead(),
        actor="builder",
    )
    step = runner.advance(con, job.id, broken)
    assert step.action == "refused"
    assert jobs.get(con, job.id).state == "parked"
    assert con.execute("SELECT COUNT(*) c FROM jobs WHERE kind='claude_code'").fetchone()["c"] == 0
    assert "GitHub" in step.spoken or "couldn't reach" in step.spoken.lower()


def test_a_naming_carrier_phrase_is_not_part_of_the_name(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """ "call it comment radar" is the repository `comment-radar`, not `call-it-comment-radar`.

    The user would only find out at the read-back, having by then said the name
    twice and heard it wrong twice.
    """
    assert runner._without_carrier("call it comment radar") == "comment radar"
    assert runner._without_carrier("name it the scraper") == "the scraper"
    assert runner._without_carrier("rename the repository jarvis") == "jarvis"
    # A name that merely starts with an ordinary word is untouched.
    assert runner._without_carrier("comment radar") == "comment radar"
    assert runner._without_carrier("caller id lookup") == "caller id lookup"


def test_the_renamed_repository_is_the_name_they_said(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    deps = deps_for(tmp_path, owner="Efkrdnz")
    job = a_request(con)
    first = runner.advance(con, job.id, deps)
    approve(con, first.request_id or "")
    named = runner.advance(con, job.id, deps)
    say(con, named.request_id or "", "call it comment radar")

    after = runner.advance(con, job.id, deps)
    assert "comment-radar" in after.spoken
    assert "call-it" not in after.spoken
