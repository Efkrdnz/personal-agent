""" "Let's build an app called comment watcher", end to end, with nothing real touched.

The stage's claim is an ORDER: the repository exists, private and empty, BEFORE
there is a job to write code in it. So the first test walks the whole thing —
spoken name, read-back, yes, creation, clone, job — and asserts the order from the
rows rather than from the calls.

After that, each way it can go wrong: the name is taken before the question and
after it, the yes never comes, the create gets no answer, and the same yes is
replayed by a process that restarted. Every test runs against
``jarvis.github.transport.FakeTransport`` and ``jarvis.project.workspace.FakeGit``;
nothing here can reach GitHub even if it tried.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import effects as fx
from jarvis import jobs
from jarvis import requests as rq
from jarvis.db import connect, migrate
from jarvis.github import scopes
from jarvis.github.transport import FakeTransport, Forbidden, TransportError
from jarvis.project import compensate as cp
from jarvis.project import confirm
from jarvis.project import lifecycle as lc
from jarvis.project import outbox as ob
from jarvis.project import workspace as ws
from jarvis.telegram import channel as tg

OWNER = "Efkrdnz"
SPOKEN = "comment watcher"
NAME = "comment-watcher"
FULL = f"{OWNER}/{NAME}"


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_handler_left_behind() -> Iterator[None]:
    before = fx.handler_for(cp.COMPENSATE_OP)
    yield
    if before is None:
        fx._HANDLERS.pop(cp.COMPENSATE_OP, None)
    else:
        fx._HANDLERS[cp.COMPENSATE_OP] = before


def a_transport(*, scopes_held: tuple[str, ...] | None = ("repo",)) -> FakeTransport:
    return FakeTransport(login=OWNER, scopes=scopes_held)


def a_config(t: FakeTransport, root: Path) -> lc.ProjectConfig:
    return lc.ProjectConfig(owner=OWNER, capabilities=scopes.capabilities(t), workspace_root=root)


def say_yes(con: sqlite3.Connection, request_id: str) -> None:
    """Answer the way the Telegram channel would, through its own builder."""
    req = rq.get_request(con, request_id)
    assert req is not None
    assert rq.answer_request(con, req.id, tg.build_answer(req, picks=(1,)), "telegram", "button")


def say(con: sqlite3.Connection, request_id: str, words: str) -> None:
    req = rq.get_request(con, request_id)
    assert req is not None
    assert rq.answer_request(
        con, req.id, tg.build_answer(req, free_text=words), "telegram", "button"
    )


# ───────────────────────────── the whole slice ─────────────────────────────


def test_a_spoken_name_becomes_a_repository_then_a_clone_then_a_job(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    config = a_config(t, tmp_path / "projects")

    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    assert proposal.state == "confirming"
    assert proposal.name == NAME
    assert f"github.com/{FULL}" in proposal.spoken
    # NOTHING has happened on GitHub yet, and nothing in the ledger.
    assert t.writes == []
    assert fx.recent_effects(con) == []

    say_yes(con, str(proposal.request_id))
    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "created", made.spoken
    assert made.full_name == FULL
    assert made.repo is not None and made.repo.private is True

    # Private and empty, and asked for that way rather than hoped for.
    created_call = t.sent("POST")[0]
    assert created_call.body == {"name": NAME, "private": True, "auto_init": False}
    assert created_call.retry_safe is False

    # Two rows: the irreversible fact, and the compensatable one.
    created = fx.get_effect(con, str(made.create_effect_id))
    live = fx.get_effect(con, str(made.live_effect_id))
    assert created is not None and live is not None
    assert created.kind == lc.REPO_CREATE_KIND
    assert created.reversibility == "irreversible"
    assert created.undo_plan is None
    assert live.kind == lc.REPO_LIVE_KIND
    assert live.reversibility == "compensatable"
    assert (live.undo_plan or {})["op"] == cp.COMPENSATE_OP
    # Both point back at the read-back the user actually answered.
    assert created.confirmed_by_request_id == proposal.request_id
    assert live.confirmed_by_request_id == proposal.request_id

    # The outbox row is spent, done, and linked to the effect that proves it.
    row = ob.get(con, str(made.outbox_id))
    assert row is not None
    assert (row.state, row.attempts, row.at_most_once) == ("done", 1, True)
    assert row.effect_id == created.id

    # THEN the clone, and THEN the job — in that order.
    git = ws.FakeGit()
    space = lc.prepare_workspace(config=config, name=NAME, git=git, token="ghp_" + "x" * 36)
    assert space.state == "cloned"
    assert space.path == tmp_path / "projects" / NAME

    job = lc.start_build_job(
        con,
        config=config,
        name=NAME,
        spoken_name=SPOKEN,
        cwd=space.path,
        created_by="desk",
        prompt_text="build a comment watcher",
    )
    assert job.kind == "claude_code"
    assert job.title == SPOKEN  # spoken, so the words the user said
    assert job.cwd == str(space.path)
    assert job.repo == FULL
    assert job.state == "queued"
    assert job.cc_session_id  # stage 2 can resume it after a reboot

    # The order is checkable from the ledger: the effect precedes the job row.
    assert created.ts <= job.created_at


def test_the_confirmation_is_the_only_authorisation(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")

    # Still pending: nothing is created and no outbox row is written.
    pending = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert pending.state == "not_confirmed"
    assert pending.decision is not None and pending.decision.kind == "pending"
    assert t.writes == []
    assert con.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"] == 0


def test_a_no_creates_nothing(con: sqlite3.Connection, tmp_path: Path) -> None:
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    req = rq.get_request(con, str(proposal.request_id))
    assert req is not None
    rq.answer_request(con, req.id, tg.build_answer(req, picks=(2,)), "telegram", "button")

    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "not_confirmed"
    assert t.writes == []


def test_one_yes_can_never_authorise_two_repositories(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two independent refusals: the request is consumed, and the row is spent."""
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))

    first = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    again = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert first.state == "created"
    assert again.state == "created"  # a replay, not a second creation
    assert again.create_effect_id == first.create_effect_id
    assert len(t.sent("POST")) == 1
    assert len(fx.recent_effects(con, state="applied")) == 2  # the same two rows


# ───────────────────────────── collisions ─────────────────────────────


def test_a_name_already_taken_asks_for_another_one_instead_of_suffixing(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    t.add_repo(FULL)
    config = a_config(t, tmp_path)

    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    assert proposal.state == "name_taken"
    assert "already a repository called comment-watcher" in proposal.spoken
    assert "not going to add a number" in proposal.spoken
    assert f"{NAME}-2" not in proposal.spoken
    assert t.writes == []

    req = rq.get_request(con, str(proposal.request_id))
    assert req is not None and req.kind == "free_text"

    # The spoken rename, and a second trip through propose with the new words.
    say(con, req.id, "yorum izleyici")
    answered = rq.get_request(con, req.id)
    assert answered is not None
    new_words = confirm.spoken_name_from(answered)
    assert new_words == "yorum izleyici"

    second = lc.propose(con, config=config, spoken_name=new_words, transport=t, actor="desk")
    assert second.state == "confirming"
    assert second.name == "yorum-izleyici"


def test_a_name_taken_between_the_question_and_the_call_reopens_the_loop(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """The 422 is the only reliable collision signal: a private repo answers 404 to a read."""
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))

    t.add_repo(FULL)  # somebody else got there first
    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "collision"
    assert "between my asking you and my creating it" in made.spoken
    assert made.request_id != proposal.request_id
    renamed = rq.get_request(con, str(made.request_id))
    assert renamed is not None and renamed.kind == "free_text"

    row = ob.get(con, str(made.outbox_id))
    assert row is not None and row.state == "failed"  # provably nothing happened
    assert ob.needs_human(con) == []
    assert fx.recent_effects(con) == []


# ───────────────────────────── when GitHub does not answer ─────────────────────────────


def test_an_ambiguous_create_goes_to_a_human_and_is_never_tried_again(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """The reason the outbox column exists: a retry here is a second repository."""
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))
    t.script["POST /user/repos"] = [TransportError("connection reset")]

    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "needs_human"
    assert "don't know whether it exists" in made.spoken
    assert "not going to ask again" in made.spoken

    row = ob.get(con, str(made.outbox_id))
    assert row is not None and row.state == "needs_human"
    assert [r.id for r in ob.needs_human(con)] == [row.id]

    # Running the row again — a worker, a restart, a hopeful human — does nothing.
    again = lc.run_repo_create(con, row_id=row.id, transport=t, config=config, actor="worker")
    assert again.state == "needs_human"
    assert len(t.sent("POST")) == 1


def test_a_refusal_says_what_github_said_and_creates_no_effect_row(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))
    t.script["POST /user/repos"] = [
        Forbidden(403, "Resource not accessible by personal access token")
    ]

    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "refused"
    assert "403" in made.spoken
    assert fx.recent_effects(con) == []
    row = ob.get(con, str(made.outbox_id))
    assert row is not None and row.state == "failed"


def test_a_repository_created_public_is_an_emergency_not_a_success(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """The one safety property is "private and empty". If it did not hold, say so.

    The spine refuses to record a creation that is not private and empty — rightly
    — so the ledger's record of this is the compensatable row, and the outbox row
    waits for a human.
    """
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))
    # The fake honours `private` from the body, so a policy that ignores it is
    # modelled by scripting the response GitHub would have sent.
    t.script["POST /user/repos"] = [
        {
            "name": NAME,
            "full_name": FULL,
            "node_id": "R_public",
            "html_url": f"https://github.com/{FULL}",
            "owner": {"login": OWNER},
            "private": False,
            "archived": False,
            "size": 0,
        }
    ]

    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "needs_human"
    assert "PUBLIC" in made.spoken
    assert made.create_effect_id is None
    live = fx.get_effect(con, str(made.live_effect_id))
    assert live is not None and live.reversibility == "compensatable"
    row = ob.get(con, str(made.outbox_id))
    assert row is not None and row.state == "needs_human"


# ───────────────────────────── refusals before anything is asked ───────────────────


def test_a_name_that_cannot_be_slugged_is_refused_without_asking_github(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    config = a_config(t, tmp_path)
    before = len(t.calls)  # reading the matrix cost one GET /user; that is all so far
    proposal = lc.propose(con, config=config, spoken_name="...", transport=t, actor="desk")
    assert proposal.state == "cannot_name"
    assert proposal.spoken
    assert t.calls[before:] == []  # GitHub was not asked about a name that does not exist
    assert rq.open_requests(con) == []


def test_a_github_that_cannot_be_reached_creates_nothing_and_says_so(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    config = a_config(t, tmp_path)
    t.script[f"GET /repos/{FULL}"] = [TransportError("dns failure")]
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    assert proposal.state == "cannot_check"
    assert "haven't created anything" in proposal.spoken
    assert rq.open_requests(con) == []


def test_a_token_that_cannot_create_says_why_before_asking_anything(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport(scopes_held=("public_repo",))
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    assert proposal.state == "cannot_create"
    assert "public_repo" in proposal.spoken
    assert rq.open_requests(con) == []


# ───────────────────────────── undo, from the top ─────────────────────────────


def test_undo_that_after_a_build_finds_the_row_something_can_be_done_about(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport()
    config = a_config(t, tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))
    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    cp.register_repo_compensator(t, config.capabilities)

    target = lc.undoable_repo_effect(con, repo_full_name=FULL)
    assert target is not None and target.kind == lc.REPO_LIVE_KIND

    result = lc.undo_repo(con, actor="desk", repo_full_name=FULL)
    assert result.outcome == "undone"
    assert f"{OWNER}/zz-abandoned-{NAME}".lower() in t.repos

    # The irreversible row still stands, and still says what it always said.
    created = fx.get_effect(con, str(made.create_effect_id))
    assert created is not None and created.state == "applied"
    assert "nothing I can do" in fx.spoken_effect_line(created)


def test_undo_with_no_compensation_available_speaks_the_irreversible_refusal(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    t = a_transport(scopes_held=("repo",))
    nothing = scopes.Capabilities(
        token_kind="classic",
        create="yes",
        delete="no",
        archive="no",
        rename="no",
        set_private="no",
        login=OWNER,
        scopes=("repo",),
    )
    config = lc.ProjectConfig(owner=OWNER, capabilities=nothing, workspace_root=tmp_path)
    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    say_yes(con, str(proposal.request_id))
    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "created"
    assert made.live_effect_id is None  # nothing to offer, so nothing is offered
    assert "nothing I can do" in made.spoken

    result = lc.undo_repo(con, actor="desk", repo_full_name=FULL)
    assert result.outcome == "irreversible"
    assert "nothing I can do" in result.spoken
    assert t.writes == [t.sent("POST")[0]]  # only the create ever wrote anything


def test_a_job_row_is_the_whole_handover_to_the_driver(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """Stage 2 runs inside the clone unchanged, which means: cwd, and nothing else."""
    t = a_transport()
    config = a_config(t, tmp_path)
    job = lc.start_build_job(
        con,
        config=config,
        name=NAME,
        spoken_name=SPOKEN,
        cwd=tmp_path / NAME,
        created_by="desk",
        model="opus",
        effort="xhigh",
    )
    fetched = jobs.get(con, job.id)
    assert fetched is not None
    assert fetched.cwd == str(tmp_path / NAME)
    assert fetched.model == "opus"
    assert fetched.effort == "xhigh"
    assert fetched.permission_mode is None  # never 'dontAsk'; the schema refuses it


def test_the_config_refuses_the_cloud(tmp_path: Path) -> None:
    t = a_transport()
    with pytest.raises(ValueError, match="ADR 0009"):
        lc.ProjectConfig(
            owner=OWNER,
            capabilities=scopes.capabilities(t),
            workspace_root=tmp_path,
            host="cloud",  # type: ignore[arg-type]
        )


def test_the_config_comes_from_the_file_but_the_matrix_never_does(tmp_path: Path) -> None:
    """A hand-edited capability matrix is the one thing that could make the line lie."""
    t = a_transport()
    caps = scopes.capabilities(t)
    config = lc.ProjectConfig.from_mapping(
        {"owner": OWNER, "workspace_root": str(tmp_path), "nonsense": 1, "capabilities": {}},
        capabilities=caps,
    )
    assert config.owner == OWNER
    assert config.workspace_root == tmp_path
    assert config.capabilities is caps


# ───────────────────── review regression: the most capable token ─────────────────────


def test_a_token_that_can_also_delete_still_creates_and_still_gets_a_live_row(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    """A classic PAT holding ``repo`` AND ``delete_repo`` is an ordinary token.

    Every other test here runs on ``("repo",)``, and the delete-capable matrix was
    the one nothing exercised. It used to reach :func:`jarvis.effects.record_effect`
    with ``irreversible`` and a plan and raise ValueError — AFTER the POST had
    already created the repository, leaving it live on the account with the outbox
    row stuck ``inflight`` and the caller holding a traceback instead of a sentence.
    """
    t = a_transport(scopes_held=("repo", "delete_repo"))
    config = a_config(t, tmp_path / "projects")
    assert config.capabilities.delete == "yes"

    proposal = lc.propose(con, config=config, spoken_name=SPOKEN, transport=t, actor="desk")
    assert proposal.state == "confirming"
    say_yes(con, str(proposal.request_id))

    made = lc.create(
        con, config=config, request_id=str(proposal.request_id), transport=t, actor="desk"
    )
    assert made.state == "created", made.spoken
    assert made.live_effect_id is not None, "a token that can compensate must get the live row"

    live = fx.get_effect(con, made.live_effect_id)
    assert live is not None
    # Never `reversible`: nothing in this package deletes a repository, whatever
    # the scope header says, so a row promising it would offer an undo no handler
    # performs.
    assert live.reversibility == "compensatable"
    assert (live.undo_plan or {}).get("op") == cp.COMPENSATE_OP

    # And the row the outbox wrote agrees that the work finished.
    row = ob.get(con, str(made.outbox_id))
    assert row is not None and row.state == "done"
    assert ob.needs_human(con) == []
