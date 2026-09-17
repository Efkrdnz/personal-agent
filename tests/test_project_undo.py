"""'undo that' on a repository: what it does, and what it admits it could not do.

This is the stage's exit criterion, so the tests are about the SENTENCE as much
as the calls. A partial compensation that reports success is the failure mode, so
every partial case here asserts that the missing step is named out loud.

The honesty checks are written so that softening the wording fails the suite:
the promise vocabulary comes from :mod:`jarvis.effects` (the spine's own list),
and the templates are swapped for over-promising ones to prove the guard fires
rather than merely existing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import effects as fx
from jarvis.db import connect, migrate
from jarvis.github import scopes
from jarvis.github.transport import (
    FakeTransport,
    Forbidden,
    NotFound,
    TransportError,
    Unprocessable,
)
from jarvis.ids import now
from jarvis.project import compensate as cp
from jarvis.project import lifecycle as lc

OWNER = "Efkrdnz"
NAME = "comment-watcher"
FULL = f"{OWNER}/{NAME}"
ABANDONED = f"zz-abandoned-{NAME}"
JOB = "job_undotests"


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    c.execute(
        """INSERT INTO jobs (id, kind, title, state, created_at, updated_at, created_by)
           VALUES (?, 'claude_code', 'comment watcher', 'running', ?, ?, 'test')""",
        (JOB, now(), now()),
    )
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_handler_left_behind() -> Iterator[None]:
    """The spine's registry is process-wide code. Leaving ours in it leaks into other tests."""
    before = fx.handler_for(cp.COMPENSATE_OP)
    yield
    if before is None:
        fx._HANDLERS.pop(cp.COMPENSATE_OP, None)
    else:
        fx._HANDLERS[cp.COMPENSATE_OP] = before


def transport(*, scopes_held: tuple[str, ...] | None = ("repo",)) -> FakeTransport:
    t = FakeTransport(login=OWNER, scopes=scopes_held)
    t.add_repo(FULL, private=True)
    return t


def caps_of(t: FakeTransport) -> scopes.Capabilities:
    return scopes.capabilities(t)


def live_row(con: sqlite3.Connection, caps: scopes.Capabilities) -> fx.Effect:
    """The compensatable row, written the way the lifecycle writes it."""
    plan = cp.plan_for(caps, owner=OWNER, slug=NAME)
    assert plan is not None
    return fx.record_effect(
        con,
        kind=lc.REPO_LIVE_KIND,
        summary=f"I made {FULL} the live repository for this project",
        reversibility=cp.ledger_class(caps),
        job_id=JOB,
        provider_ref={"full_name": FULL, "name": NAME, "owner": OWNER},
        undo_plan=plan,
    )


# ───────────────────────────── the whole compensation ─────────────────────────────


def test_undo_runs_every_step_the_token_has_and_links_the_new_effect(
    con: sqlite3.Connection,
) -> None:
    t = transport()
    caps = caps_of(t)
    cp.register_repo_compensator(t, caps)
    effect = live_row(con, caps)

    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "undone"
    assert result.undo_effect_id is not None

    # The repository really moved, and the account holds exactly one of them.
    assert f"{OWNER}/{ABANDONED}".lower() in t.repos
    assert FULL.lower() not in t.repos
    assert t.repos[f"{OWNER}/{ABANDONED}".lower()]["archived"] is True
    assert t.repos[f"{OWNER}/{ABANDONED}".lower()]["private"] is True

    # And the ledger links the compensation to what it compensated.
    after = fx.get_effect(con, effect.id)
    compensation = fx.get_effect(con, result.undo_effect_id)
    assert after is not None and compensation is not None
    assert after.state == "undone"
    assert after.undo_effect_id == compensation.id
    assert compensation.kind == cp.COMPENSATION_KIND
    assert (compensation.provider_ref or {})["undo_of"] == effect.id
    assert (compensation.provider_ref or {})["renamed_to"] == ABANDONED


def test_the_rename_happens_before_the_archive_or_nothing_after_it_would_work(
    con: sqlite3.Connection,
) -> None:
    """An archived repository is read-only, to this token as much as anyone's.

    The fake answers 403 "archived so is read-only" to a PATCH on an archived
    repository, exactly as GitHub does, so a reordering of
    ``jarvis.github.repos.COMPENSATION_ORDER`` fails here.
    """
    t = transport()
    cp.register_repo_compensator(t, caps_of(t))
    fx.undo(con, live_row(con, caps_of(t)).id, actor="desk")

    patched = [c for c in t.calls if c.method == "PATCH"]
    bodies = [c.body or {} for c in patched]
    assert "name" in bodies[0], bodies
    assert bodies[-1].get("archived") is True, bodies
    assert cp.gh.COMPENSATION_ORDER[-1] == "archive"


def test_the_spoken_line_says_what_was_done_and_that_deletion_is_not_possible(
    con: sqlite3.Connection,
) -> None:
    t = transport()
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))
    result = fx.undo(con, effect.id, actor="desk")

    assert result.undo_effect_id is not None
    compensation = fx.get_effect(con, result.undo_effect_id)
    assert compensation is not None
    said = fx.spoken_effect_line(compensation)
    assert f"renamed it to {ABANDONED}" in said
    assert "made it private" in said
    assert "archived it" in said
    assert "can't delete it" in said
    assert "delete_repo" in said


# ───────────────────────────── partial compensation ─────────────────────────────


def test_a_step_that_fails_is_named_out_loud(con: sqlite3.Connection) -> None:
    """Archive succeeds, rename fails: the line has to say exactly that."""
    t = transport()
    t.script["PATCH /repos/Efkrdnz/comment-watcher"] = [
        Unprocessable(
            422,
            "Repository creation failed.",
            errors=({"message": "name already exists on this account"},),
        )
    ]
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))

    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "undone"  # something WAS done
    assert result.undo_effect_id is not None
    compensation = fx.get_effect(con, result.undo_effect_id)
    assert compensation is not None

    said = compensation.summary
    assert "could not" in said
    assert ABANDONED in said
    assert "is taken too" in said
    assert "made it private" in said or "archived it" in said
    assert cp.shortfall(compensation) == ("rename",)
    # The repository kept its name, and the ledger does not pretend otherwise.
    assert (compensation.provider_ref or {})["renamed_to"] is None
    assert FULL.lower() in t.repos


def test_a_compensation_that_did_nothing_at_all_is_not_recorded_as_success(
    con: sqlite3.Connection,
) -> None:
    """Every step cleanly refused -> undo_failed, retryable, and honest about it."""
    t = transport()
    t.script["PATCH /repos/Efkrdnz/comment-watcher"] = [
        Forbidden(403, "Resource not accessible by personal access token") for _ in range(3)
    ]
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))

    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "failed"
    assert result.undo_effect_id is None
    after = fx.get_effect(con, effect.id)
    assert after is not None
    assert after.state == "undo_failed"
    assert "403" in (after.undo_error or "")
    assert "nothing has changed" in fx.spoken_effect_line(after)


def test_a_step_with_no_answer_is_never_retried_and_never_claimed_as_done(
    con: sqlite3.Connection,
) -> None:
    """A rename that may or may not have happened is the double-effect hazard again."""
    t = transport()
    t.script["PATCH /repos/Efkrdnz/comment-watcher"] = [TransportError("connection reset")]
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))

    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "undone"
    assert result.undo_effect_id is not None
    compensation = fx.get_effect(con, result.undo_effect_id)
    assert compensation is not None
    assert cp.shortfall(compensation) == ("rename",)
    assert cp.UNSURE in compensation.summary
    assert "don't know whether it happened" in compensation.summary
    # Exactly one rename was attempted, ever.
    renames = [c for c in t.calls if c.method == "PATCH" and "name" in (c.body or {})]
    assert len(renames) == 1


def test_asking_twice_compensates_once(con: sqlite3.Connection) -> None:
    t = transport()
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))

    first = fx.undo(con, effect.id, actor="desk")
    second = fx.undo(con, effect.id, actor="telegram")
    assert first.outcome == "undone"
    assert second.outcome == "already_undone"
    assert second.undo_effect_id == first.undo_effect_id
    assert len([c for c in t.calls if c.method == "PATCH"]) == 3


# ───────────────────────────── when nothing can be done ─────────────────────────────


def test_a_token_that_can_do_nothing_gets_no_plan_and_says_so(con: sqlite3.Connection) -> None:
    """The roadmap's exit condition: if archive and rename also fail, the wording changes."""
    t = FakeTransport(login=OWNER, scopes=())
    caps = caps_of(t)
    assert caps.available_compensations == ()
    assert cp.plan_for(caps, owner=OWNER, slug=NAME) is None
    assert cp.ledger_class(caps) == "irreversible"

    created = fx.github_repo_create_effect(
        con, full_name=FULL, private=True, empty=True, job_id=JOB
    )
    said = fx.spoken_effect_line(created)
    assert "nothing I can do" in said
    assert "delete_repo" in said
    result = fx.undo(con, created.id, actor="desk")
    assert result.outcome == "irreversible"
    assert t.writes == []  # nothing was attempted


def test_a_token_whose_scopes_cannot_be_read_offers_to_TRY(con: sqlite3.Connection) -> None:
    """A fine-grained token reports nothing, and "unknown" is not "no".

    Refusing to store a plan for it would make the confirmation say "I'm not sure
    I could archive it" and the undo say "there is nothing I can do" — two
    different answers to the same question.
    """
    t = transport(scopes_held=None)
    caps = caps_of(t)
    assert caps.unknown_compensations == scopes.COMPENSATIONS
    plan = cp.plan_for(caps, owner=OWNER, slug=NAME)
    assert plan is not None
    assert plan["speaks"].startswith("try to ")
    assert cp.ledger_class(caps) == "compensatable"

    cp.register_repo_compensator(t, caps)
    effect = live_row(con, caps)
    assert "try to" in fx.spoken_effect_line(effect)
    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "undone"
    assert f"{OWNER}/{ABANDONED}".lower() in t.repos


def test_a_process_that_never_registered_the_handler_says_so_honestly(
    con: sqlite3.Connection,
) -> None:
    """The registry holds CODE, so a process that did not import it reports no_handler."""
    t = transport()
    caps = caps_of(t)
    effect = live_row(con, caps)
    fx._HANDLERS.pop(cp.COMPENSATE_OP, None)

    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "no_handler"
    assert t.writes == []
    after = fx.get_effect(con, effect.id)
    assert after is not None and after.state == "undo_failed"


def test_a_step_the_token_has_since_lost_is_skipped_rather_than_attempted(
    con: sqlite3.Connection,
) -> None:
    """A plan written last month, a token re-scoped since. The matrix wins."""
    t = transport()
    generous = caps_of(t)
    effect = live_row(con, generous)

    narrowed = scopes.Capabilities(
        token_kind="classic",
        create="yes",
        delete="no",
        archive="yes",
        rename="no",
        set_private="no",
        login=OWNER,
        scopes=("repo",),
    )
    cp.register_repo_compensator(t, narrowed)
    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "undone"
    assert result.undo_effect_id is not None
    compensation = fx.get_effect(con, result.undo_effect_id)
    assert compensation is not None
    assert (compensation.provider_ref or {})["done"] == ["archive"]
    assert FULL.lower() in t.repos  # never renamed
    renames = [c for c in t.calls if c.method == "PATCH" and "name" in (c.body or {})]
    assert renames == []


# ───────────────────────────── the wording, mechanically ─────────────────────────────


def test_no_compensation_summary_may_claim_restoration() -> None:
    """The spine's own vocabulary, applied to the sentence this module generates."""
    said = cp.spoken_summary(
        FULL, done=("rename", "set_private", "archive"), failed=(), rename_to=ABANDONED
    )
    low = said.lower()
    assert not [w for w in fx.EXACT_WORDS if w in low], said


def test_the_honesty_guard_fires_rather_than_merely_existing() -> None:
    for lie in (
        "On Efkrdnz/comment-watcher I put it back exactly as it was.",
        "On Efkrdnz/comment-watcher I archived it.",
        "I restored it.",
    ):
        with pytest.raises(fx.OverPromise):
            cp.assert_honest(lie)
    # And the honest version passes.
    cp.assert_honest(f"On {FULL} I archived it. {cp.DELETE_TRUTH}.")


def test_the_after_sentence_and_the_before_sentence_describe_the_same_actions() -> None:
    """Two modules own the two halves, so the phrases have to agree on the words.

    ``jarvis.github.scopes`` writes what Jarvis can do BEFORE acting; this package
    writes what it did afterwards. If one of them is reworded, this fails — which
    is the cheapest way to keep "I can archive it" and "I archived it" about the
    same operation.
    """
    caps = scopes.Capabilities(
        token_kind="classic",
        create="yes",
        delete="no",
        archive="yes",
        rename="yes",
        set_private="yes",
        scopes=("repo",),
    )
    before = scopes.spoken_capability_line(caps, slug=NAME).lower()
    for present, past in (
        ("archive it", "archived it"),
        (f"rename it to {ABANDONED}", f"renamed it to {ABANDONED}"),
        ("make it private", "made it private"),
    ):
        assert present in before
        after = cp.spoken_summary(FULL, done=(), failed=(), rename_to=ABANDONED)
        assert past not in after  # nothing was done, so nothing is claimed
    everything = cp.spoken_summary(
        FULL, done=("archive", "rename", "set_private"), failed=(), rename_to=ABANDONED
    ).lower()
    assert "archived it" in everything
    assert f"renamed it to {ABANDONED}" in everything
    assert "made it private" in everything


def test_a_plan_with_no_repository_in_it_is_refused_rather_than_guessed(
    con: sqlite3.Connection,
) -> None:
    t = transport()
    handler = cp.repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))
    with pytest.raises(ValueError, match="names no repository"):
        handler(con, effect, {})
    assert t.writes == []


# ───────────────────────── review regressions: the two dishonest readings ─────────────────────────


def test_a_step_with_no_answer_is_never_spoken_as_a_step_that_failed() -> None:
    """ "I could not rename it (I don't know whether it happened)" is two answers.

    :data:`cp.UNSURE` exists because "I could not" and "I don't know" call for
    different actions from the human who hears them, so folding the unsure step
    into the "I could not …" clause destroys the distinction the constant was
    created to preserve. The definite half is the half acted on: the user hears
    the repository is still called ``comment-watcher``, goes looking for it under
    that name, or gives the abandoned name to something else.
    """
    said = cp.spoken_summary(FULL, done=(), failed=(), unsure=("rename",), rename_to=ABANDONED)
    assert cp.UNSURE in said
    assert "could not" not in said.lower()
    assert f"tried to rename it to {ABANDONED}" in said

    # And the same when other steps really did succeed.
    mixed = cp.spoken_summary(
        FULL, done=("set_private", "archive"), failed=(), unsure=("rename",), rename_to=ABANDONED
    )
    assert "made it private and archived it" in mixed
    assert "could not" not in mixed.lower()
    assert cp.UNSURE in mixed


def test_a_refused_step_and_an_unanswered_step_are_told_apart_in_one_sentence() -> None:
    """The mixed case is the one a channel cannot re-derive from the row alone."""
    said = cp.spoken_summary(
        FULL,
        done=("archive",),
        failed=(("set_private", "403 Forbidden"),),
        unsure=("rename",),
        rename_to=ABANDONED,
    )
    assert "I archived it, but I could not make it private (403 Forbidden)" in said
    assert f"I tried to rename it to {ABANDONED}, but got {cp.UNSURE}" in said


def test_a_404_after_an_unanswered_rename_says_the_repository_may_have_moved(
    con: sqlite3.Connection,
) -> None:
    """A bare "404 Not Found" here is accurate and useless.

    The rename got no answer, so the repository is under one of two names and we
    addressed the other. Saying so is what stops a human hunting for a fault that
    is not there.
    """
    t = transport()
    t.script[f"PATCH /repos/{FULL}"] = [
        TransportError("connection reset"),
        NotFound(404, "Not Found"),
        NotFound(404, "Not Found"),
    ]
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))

    result = fx.undo(con, effect.id, actor="desk")
    assert result.outcome == "undone"
    assert result.undo_effect_id is not None
    compensation = fx.get_effect(con, result.undo_effect_id)
    assert compensation is not None
    assert f"the rename to {ABANDONED} may have gone through after all" in compensation.summary
    assert "404 Not Found" not in compensation.summary
    assert sorted(cp.shortfall(compensation)) == ["archive", "rename", "set_private"]


def test_the_ledger_class_and_the_plan_can_never_disagree(con: sqlite3.Connection) -> None:
    """They are one decision, and a disagreement is not a wrong sentence.

    :func:`jarvis.effects.record_effect` refuses a plan on an irreversible row, so
    a class that says "irreversible" while :func:`cp.plan_for` hands over a plan
    raises — AFTER the repository exists on GitHub. Every matrix is checked, not
    just the ones the happy path uses.
    """
    tri = ("yes", "no", "unknown")
    for delete in tri:
        for archive in tri:
            for rename in tri:
                for private in tri:
                    caps = scopes.Capabilities(
                        token_kind="classic",
                        create="yes",
                        delete=delete,
                        archive=archive,
                        rename=rename,
                        set_private=private,
                    )
                    plan = cp.plan_for(caps, owner=OWNER, slug=NAME)
                    klass = cp.ledger_class(caps)
                    assert klass != "reversible"
                    assert (plan is not None) == (klass == "compensatable"), (
                        f"delete={delete} archive={archive} rename={rename} private={private}: "
                        f"plan={plan is not None} class={klass}"
                    )
                    # The pairing has to be one record_effect will actually accept.
                    row = fx.record_effect(
                        con,
                        kind=lc.REPO_LIVE_KIND,
                        summary=f"I made {FULL} the live repository for this project",
                        reversibility=klass,
                        undo_plan=plan,
                        provider_ref={"full_name": FULL},
                    )
                    assert row.reversibility == klass


def test_a_partial_compensation_cannot_report_success_through_undo_repo(
    con: sqlite3.Connection,
) -> None:
    """The stage's headline failure mode, checked on the field a channel speaks.

    :func:`jarvis.effects.undo` builds ``spoken`` from the row it undid, so a
    partial compensation came back saying "that one is already dealt with" while
    the repository sat there under its original name. The shortfall was written
    down — on the compensation row, which the caller had to know to go and fetch.
    """
    t = transport()
    t.script[f"PATCH /repos/{FULL}"] = [Unprocessable(422, "name already exists on this account")]
    cp.register_repo_compensator(t, caps_of(t))
    live_row(con, caps_of(t))

    result = lc.undo_repo(con, actor="desk", repo_full_name=FULL)
    assert result.outcome == "undone"
    assert "already dealt with" not in result.spoken
    assert f"I could not rename it to {ABANDONED}" in result.spoken
    assert "still there" in result.spoken
    # And it is the compensation's own sentence, not a second one that could drift.
    compensation = fx.get_effect(con, str(result.undo_effect_id))
    assert compensation is not None
    assert result.spoken == compensation.summary


def test_a_complete_compensation_still_says_what_it_could_not_do(
    con: sqlite3.Connection,
) -> None:
    """Every step worked and the repository is STILL THERE. That has to be audible."""
    t = transport()
    cp.register_repo_compensator(t, caps_of(t))
    effect = live_row(con, caps_of(t))

    result = lc.undo_repo(con, actor="desk", effect_id=effect.id)
    assert result.outcome == "undone"
    assert f"renamed it to {ABANDONED}" in result.spoken
    assert "made it private" in result.spoken and "archived it" in result.spoken
    cp.assert_honest(result.spoken)


def test_a_refusal_keeps_the_spine_s_own_wording(con: sqlite3.Connection) -> None:
    """No compensation row, nothing to swap in — the honest refusal must survive."""
    t = FakeTransport(login=OWNER, scopes=())
    created = fx.github_repo_create_effect(
        con, full_name=FULL, private=True, empty=True, job_id=JOB
    )
    result = lc.undo_repo(con, actor="desk", effect_id=created.id)
    assert result.outcome == "irreversible"
    assert result.undo_effect_id is None
    assert "nothing I can do" in result.spoken
    assert t.writes == []
