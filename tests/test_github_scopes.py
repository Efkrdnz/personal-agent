"""The capability matrix and the sentence it generates, tested for honesty.

This file is the stage-4 equivalent of the ``spoken_effect_line`` tests in
``tests/test_effects.py``, and it is written the same way: the wording is
generated from data, every reachable combination of that data is exercised, and
the clause templates are then SWAPPED FOR LYING ONES to prove the guard fires
rather than merely existing.

The matrix has four operations and three answers each, so there are 81 reachable
combinations. All of them are generated here, because the case that matters — a
token whose permissions cannot be read at all — is the one a hand-picked example
list always forgets.

Nothing here touches api.github.com. The whole point of reading capabilities out
of a response header is that it costs one GET; the whole point of
:class:`FakeTransport` is that even that GET is never really made.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import effects as fx
from jarvis.db import connect, migrate
from jarvis.github import repos
from jarvis.github import scopes as sc
from jarvis.github.transport import FakeTransport, Response

TRI = ("yes", "no", "unknown")
SLUG = "comment-watcher"


def _caps(
    *,
    delete: str = "no",
    archive: str = "yes",
    rename: str = "yes",
    set_private: str = "yes",
    create: str = "yes",
    kind: str = "classic",
) -> sc.Capabilities:
    return sc.Capabilities(
        token_kind=kind,  # type: ignore[arg-type]
        create=create,  # type: ignore[arg-type]
        delete=delete,  # type: ignore[arg-type]
        archive=archive,  # type: ignore[arg-type]
        rename=rename,  # type: ignore[arg-type]
        set_private=set_private,  # type: ignore[arg-type]
    )


# ───────────────────────── reading it, without creating ─────────────────────────


def test_the_matrix_is_read_with_one_request_that_changes_nothing() -> None:
    """Spike S8 proposed creating a throwaway repo. A header read leaves nothing."""
    t = FakeTransport(scopes=("repo", "gist"), login="Efkrdnz")
    caps = sc.capabilities(t)
    assert [(c.method, c.path) for c in t.calls] == [("GET", "/user")]
    assert t.writes == []
    assert caps.login == "Efkrdnz"
    assert caps.source == sc.SCOPE_HEADER


def test_the_deliberate_token_can_compensate_and_cannot_delete() -> None:
    caps = sc.capabilities(FakeTransport(scopes=("repo",)))
    assert caps.delete == "no"
    assert caps.create == "yes"
    assert caps.available_compensations == ("archive", "rename", "set_private")
    assert any("delete_repo" in note for note in caps.notes)


def test_delete_repo_in_the_scope_list_is_read_as_yes() -> None:
    caps = sc.capabilities(FakeTransport(scopes=("repo", "delete_repo")))
    assert caps.delete == "yes"
    assert sc.implied_reversibility(caps) == "reversible"


def test_delete_repo_without_repo_is_unknown_rather_than_yes() -> None:
    """It grants admin rights, not visibility: whether it reaches a PRIVATE repo
    is not answerable from the scope list, and guessing is how the line lies."""
    caps = sc.capabilities(FakeTransport(scopes=("delete_repo",)))
    assert caps.delete == "unknown"
    assert caps.create == "no"


def test_public_repo_alone_cannot_create_the_private_repo_this_system_insists_on() -> None:
    caps = sc.capabilities(FakeTransport(scopes=("public_repo",)))
    assert caps.create == "no"
    assert caps.delete == "no"
    # And what it could do to a private repo it cannot even see is not knowable.
    assert caps.archive == caps.rename == caps.set_private == "unknown"


def test_a_token_with_no_repo_scopes_at_all_can_do_nothing() -> None:
    caps = sc.capabilities(FakeTransport(scopes=("gist", "read:user")))
    assert [caps.of(op) for op in sc.OPERATIONS] == ["no"] * 5


def test_a_fine_grained_token_reports_unknown_for_everything() -> None:
    """THE reason every answer is a tri-state: this is a whole class of tokens."""
    caps = sc.capabilities(FakeTransport(scopes=None, kind="fine_grained"))
    assert [caps.of(op) for op in sc.OPERATIONS] == ["unknown"] * 5
    assert caps.scopes is None
    assert caps.source == "header-absent"
    assert any("fine-grained" in note for note in caps.notes)


def test_an_absent_header_and_an_empty_one_are_not_the_same_fact() -> None:
    absent = sc.capabilities(FakeTransport(scopes=None, kind="classic"))
    empty = sc.capabilities(FakeTransport(scopes=(), kind="classic"))
    assert absent.delete == "unknown"
    assert empty.delete == "no"
    assert empty.scopes == ()
    assert empty.source == "header-empty"


def test_an_empty_header_on_a_token_we_cannot_classify_stays_unknown() -> None:
    """Reading "" as "no scopes" is only safe if we know it is a classic token."""
    caps = sc.capabilities(FakeTransport(scopes=(), kind="unknown"))
    assert caps.delete == "unknown"
    assert caps.source == "header-empty"


def test_any_authenticated_response_can_refresh_the_matrix_for_free() -> None:
    """The headers come back on the errors too, so a 403 costs nothing to learn from."""
    resp = Response(403, {"x-oauth-scopes": "repo, delete_repo"}, {"message": "no"})
    caps = sc.capabilities_from_response(resp, kind="classic", login="Efkrdnz")
    assert caps.delete == "yes"
    assert caps.login == "Efkrdnz"


def test_the_matrix_serialises_for_the_ledger_and_carries_no_credential() -> None:
    caps = sc.capabilities(FakeTransport(scopes=("repo",)))
    blob = json.dumps(caps.as_dict())
    assert '"delete": "no"' in blob
    assert "token" not in json.loads(blob) or json.loads(blob)["token_kind"] == "classic"
    assert set(sc.OPERATIONS) <= set(caps.as_dict())


def test_asking_about_an_operation_nobody_defined_raises() -> None:
    with pytest.raises(KeyError, match="no such operation"):
        _caps().of("force_push")


# ───────────────────────────── the spoken line ─────────────────────────────


def test_the_canonical_stage_four_sentence_is_generated_not_written() -> None:
    caps = _caps(delete="no", archive="yes", rename="yes", set_private="yes")
    assert sc.spoken_capability_line(caps, slug=SLUG) == (
        "I can archive it, rename it to zz-abandoned-comment-watcher and make it private, "
        "but I can't delete it."
    )


def test_a_delete_capable_token_says_it_really_can_be_undone() -> None:
    line = sc.spoken_capability_line(_caps(delete="yes"), slug=SLUG)
    assert "delete it" in line
    assert "undone" in line


def test_a_token_that_can_do_nothing_says_there_is_nothing_it_can_do() -> None:
    caps = _caps(delete="no", archive="no", rename="no", set_private="no")
    line = sc.spoken_capability_line(caps, slug=SLUG)
    assert line == "I can't delete it, so there is nothing I can do about it afterwards."


def test_an_unreadable_token_says_unknown_and_picks_neither_side() -> None:
    caps = sc.capabilities(FakeTransport(scopes=None, kind="fine_grained"))
    line = sc.spoken_capability_line(caps, slug=SLUG)
    assert "don't know" in line
    assert "not sure" in line
    # Neither a promise nor a flat refusal: it says the honest thing instead.
    assert "there is nothing I can do" not in line
    assert "assume nothing can be done" in line


def test_a_partly_known_token_offers_only_what_it_measured() -> None:
    caps = _caps(delete="no", archive="yes", rename="unknown", set_private="no")
    line = sc.spoken_capability_line(caps, slug=SLUG)
    assert line == (
        "I can archive it, but I can't delete it, and I'm not sure I could rename it to "
        "zz-abandoned-comment-watcher."
    )


def test_the_name_it_promises_is_the_name_the_compensation_would_use() -> None:
    """Wording and action must not drift: one prefix, one function, one test."""
    caps = _caps()
    line = sc.spoken_capability_line(caps, slug=SLUG)
    assert sc.abandoned_name(SLUG) in line
    # And it is a name GitHub will actually accept, i.e. rename() would not refuse it.
    assert repos.valid_slug(sc.abandoned_name(SLUG))
    assert sc.abandoned_name(SLUG) == "zz-abandoned-comment-watcher"


@pytest.mark.parametrize(
    ("delete", "archive", "rename", "set_private"),
    list(itertools.product(TRI, TRI, TRI, TRI)),
)
def test_every_reachable_matrix_produces_an_honest_sentence(
    delete: str, archive: str, rename: str, set_private: str
) -> None:
    """All 81 of them, because the forgotten combination is always the honest one.

    The generator checks itself (that is the guard), so this asserts the same
    properties INDEPENDENTLY, by reading the offer clause back out of the
    finished sentence rather than by trusting the clause logic that built it.
    """
    caps = _caps(delete=delete, archive=archive, rename=rename, set_private=set_private)
    line = sc.spoken_capability_line(caps, slug=SLUG)
    assert line.endswith(".")
    assert line[0].isupper()

    offer = _offer(line)
    if delete == "yes":
        assert "delete it" in offer
        return

    assert "delete" in line and "delete" not in offer
    for op in sc.COMPENSATIONS:
        phrase = sc._PHRASES[op].format(abandoned=sc.abandoned_name(SLUG))
        assert (phrase in offer) == (caps.of(op) == "yes"), (op, line)
    if not caps.available_compensations:
        assert offer == ""
        assert "nothing" in line


def _offer(line: str) -> str:
    """The part of the sentence that PROMISES something, parsed independently."""
    if not line.startswith("I can "):
        return ""
    rest = line[len("I can ") :]
    for boundary in (", but ", ", and ", ", so "):
        rest = rest.split(boundary)[0]
    return rest.rstrip(".")


@pytest.mark.parametrize("slug", ["delete-me", "archive-bot", "private-notes", "rename-this"])
def test_a_project_named_after_an_operation_does_not_trip_the_check(slug: str) -> None:
    """A repository name is DATA, not a promise.

    "delete-me" is a perfectly ordinary thing to call a project, and the rename
    phrase that carries it — "rename it to zz-abandoned-delete-me" — is not a
    claim that anything can be deleted. Before the name was masked out of the
    honesty check, every one of these raised.
    """
    for delete, archive, renamed, private in itertools.product(TRI, repeat=4):
        caps = _caps(delete=delete, archive=archive, rename=renamed, set_private=private)
        line = sc.spoken_capability_line(caps, slug=slug)
        if caps.of("rename") == "yes" and delete != "yes":
            assert sc.abandoned_name(slug) in line


def test_masking_the_name_does_not_blunt_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mask hides the name, not the sentence around it."""
    clauses = dict(sc._CLAUSES)
    monkeypatch.setattr(sc, "_CLAUSES", clauses)
    clauses["no_delete"] = "I will delete it later"
    with pytest.raises(fx.OverPromise, match="claims it can delete"):
        sc.spoken_capability_line(_caps(delete="no"), slug="delete-me")


# ───────────────────── the guard, proved by making it fail ─────────────────────


def test_a_clause_that_over_promises_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE regression guard, in the shape ``jarvis.effects`` established.

    Both directions: adding a promise, and quietly deleting the refusal so the
    sentence merely trails off.
    """
    clauses = dict(sc._CLAUSES)
    monkeypatch.setattr(sc, "_CLAUSES", clauses)

    clauses["no_delete"] = "I'll probably be able to delete it"
    with pytest.raises(fx.OverPromise, match="delete"):
        sc.spoken_capability_line(_caps(delete="no"), slug=SLUG)

    clauses["no_delete"] = "I can't delete it"
    clauses["nothing_certain"] = "I'll see what I can do"
    with pytest.raises(fx.OverPromise, match="refusal"):
        sc.spoken_capability_line(
            _caps(delete="no", archive="no", rename="no", set_private="no"), slug=SLUG
        )


def test_an_unknown_may_not_be_spoken_as_an_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    clauses = dict(sc._CLAUSES)
    monkeypatch.setattr(sc, "_CLAUSES", clauses)
    clauses["unsure"] = "I can also {actions}"
    with pytest.raises(fx.OverPromise, match="claims it can"):
        sc.spoken_capability_line(
            _caps(delete="no", archive="yes", rename="unknown", set_private="unknown"), slug=SLUG
        )


def test_an_unknown_may_not_be_spoken_as_certainty(monkeypatch: pytest.MonkeyPatch) -> None:
    clauses = dict(sc._CLAUSES)
    monkeypatch.setattr(sc, "_CLAUSES", clauses)
    clauses["delete_unknown"] = "I can't delete it"
    with pytest.raises(fx.OverPromise, match="sounds certain"):
        sc.spoken_capability_line(_caps(delete="unknown"), slug=SLUG)


def test_the_spine_owns_the_promise_vocabulary_this_checks_against() -> None:
    """One vocabulary, not two: a phrase added there must bind here too."""
    assert "undo" in fx.PROMISE_WORDS
    assert "exactly" in fx.EXACT_WORDS
    caps = _caps()
    for word in (*fx.PROMISE_WORDS, *fx.EXACT_WORDS):
        assert word not in _offer(sc.spoken_capability_line(caps, slug=SLUG)).lower()


def test_a_slug_that_could_shift_a_clause_boundary_is_refused() -> None:
    """Caller text may not reach the honesty check as prose."""
    for bad in ("I can't be trusted", "x, and I can delete it", "", "-leading-dash", "a" * 101):
        with pytest.raises(ValueError, match="spoken line"):
            sc.spoken_capability_line(_caps(), slug=bad)


def test_the_name_this_line_offers_is_always_a_name_the_rename_would_accept() -> None:
    """The load-bearing one: an offer that could not be performed is a lie.

    ``zz-abandoned-`` plus a 100-character slug is 113 characters, which GitHub
    refuses and :func:`repos.rename` refuses before it even asks — so a sentence
    offering that rename promises something no token could deliver, for exactly the
    long names nobody tries by hand. The promise is refused instead of made.
    """
    assert repos.MAX_SLUG_LEN - len(sc.ABANDONED_PREFIX) == sc.MAX_ABANDONED_SLUG
    assert repos.MAX_SLUG_LEN == sc.MAX_REPO_NAME
    for length in range(1, repos.MAX_SLUG_LEN + 1):
        slug = "a" * length
        try:
            line = sc.spoken_capability_line(_caps(delete="no"), slug=slug)
        except ValueError:
            assert length > sc.MAX_ABANDONED_SLUG, f"{length} characters is a usable name"
            continue
        assert length <= sc.MAX_ABANDONED_SLUG
        offered = sc.abandoned_name(slug)
        assert offered in line
        # Not "looks plausible": the very function the compensation calls.
        assert repos.valid_slug(offered)
        assert repos.check_slug(offered) == offered


def test_the_refusal_names_the_length_a_user_could_act_on() -> None:
    with pytest.raises(ValueError, match=r"88 characters.*100 characters GitHub allows"):
        sc.abandoned_name("a" * 88)


# ───────────────────── what the ledger does with it ─────────────────────


def test_the_implied_class_is_pessimistic_where_the_matrix_is_unsure() -> None:
    assert sc.implied_reversibility(_caps(delete="yes")) == "reversible"
    assert sc.implied_reversibility(_caps(delete="no")) == "compensatable"
    assert (
        sc.implied_reversibility(_caps(delete="no", archive="no", rename="no", set_private="no"))
        == "irreversible"
    )
    # Unknown everything is treated as the worst case, which is the only side
    # that is safe to be wrong on: it over-confirms rather than over-promises.
    unknown = _caps(delete="unknown", archive="unknown", rename="unknown", set_private="unknown")
    assert sc.implied_reversibility(unknown) == "irreversible"


def test_the_plan_the_capabilities_imply_is_declarative_and_ordered() -> None:
    plan = sc.implied_undo_plan(_caps(), owner="Efkrdnz", slug=SLUG)
    assert plan is not None
    assert plan["op"] == "github.repo_compensate"
    assert plan["args"]["operations"] == list(repos.COMPENSATION_ORDER)
    assert plan["args"]["rename_to"] == "zz-abandoned-comment-watcher"
    assert json.loads(json.dumps(plan)) == plan  # survives a restart, by construction
    assert (
        sc.implied_undo_plan(
            _caps(archive="no", rename="no", set_private="no"), owner="Efkrdnz", slug=SLUG
        )
        is None
    )


def test_the_execution_order_here_agrees_with_the_one_in_repos() -> None:
    """Two tuples rather than an import; this is what keeps them the same tuple."""
    assert sc._EXECUTION_ORDER == repos.COMPENSATION_ORDER
    assert set(sc._EXECUTION_ORDER) == set(sc.COMPENSATIONS)


def test_a_plan_generated_from_capabilities_is_speakable_by_the_ledger(
    con: sqlite3.Connection,
) -> None:
    """The generated ``speaks`` has to survive the spine's own write-time check.

    ``record_effect`` rejects a compensation phrase that claims restoration, and
    it rejects a plan whose op would over-promise. A phrase built here and
    refused there would be a row that can be written and never spoken.
    """
    plan = sc.implied_undo_plan(_caps(), owner="Efkrdnz", slug=SLUG)
    assert plan is not None
    e = fx.record_effect(
        con,
        kind="git.push",  # a kind the table classifies compensatable, for the check
        summary="I created the private, empty repository Efkrdnz/comment-watcher",
        reversibility="compensatable",
        undo_plan=plan,
    )
    line = fx.spoken_effect_line(e)
    assert "archive it" in line
    assert "can't reverse that" in line


def test_the_ledger_refuses_the_optimistic_class_for_a_repo_creation(
    con: sqlite3.Connection,
) -> None:
    """The reconciliation, as it actually stands — and the seam it leaves open.

    The capabilities of the deliberate token imply ``compensatable``, and
    :data:`jarvis.effects.KIND_REVERSIBILITY` says ``github.repo_create`` is
    ``irreversible``. The spine wins, deliberately: ``irreversible`` is what keeps
    the gate at ``confirm_readback``, which stage 4 requires. So the compensation
    is offered BEFORE acting, by the line this module generates, and the ledger row
    stays irreversible. Changing that is a spine decision with its own test, not
    something this package may do by passing a cheerier argument.
    """
    caps = _caps(delete="no")
    assert sc.implied_reversibility(caps) == "compensatable"
    assert fx.reversibility_of("github.repo_create") == "irreversible"
    with pytest.raises(ValueError, match="claims an undo that does not exist"):
        fx.record_effect(
            con,
            kind="github.repo_create",
            summary="I created the private, empty repository Efkrdnz/comment-watcher",
            reversibility=sc.implied_reversibility(caps),
        )
    e = fx.github_repo_create_effect(
        con, full_name="Efkrdnz/comment-watcher", private=True, empty=True
    )
    assert e.confirm_strength == "confirm_readback"


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()
