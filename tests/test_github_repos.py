"""Creating the repository, and the three things that can be done about it after.

Every test here runs against :class:`FakeTransport`. api.github.com is reachable
from this machine and there are credentials in the environment, and that is
exactly why: a repository created by a test is irreversible, visible, and on
somebody's real account.

The load-bearing test in this file is
``test_archiving_first_breaks_the_other_two_compensations``. It is the reason
:data:`jarvis.github.repos.COMPENSATION_ORDER` exists, and without it the order
would be a comment that a later refactor is free to reverse.
"""

from __future__ import annotations

import pytest

from jarvis.github import repos
from jarvis.github.scopes import abandoned_name
from jarvis.github.transport import (
    FakeTransport,
    Forbidden,
    GithubError,
    NotFound,
    Response,
    Unprocessable,
)

OWNER = "Efkrdnz"
SLUG = "comment-watcher"


def _fake(**kw: object) -> FakeTransport:
    return FakeTransport(login=OWNER, **kw)  # type: ignore[arg-type]


# ───────────────────────────── creating ─────────────────────────────


def test_a_created_repo_is_private_empty_and_returns_what_an_effect_needs() -> None:
    t = _fake()
    repo = repos.create(t, OWNER, SLUG, owner_kind="user")
    assert repo.full_name == f"{OWNER}/{SLUG}"
    assert repo.node_id and repo.html_url == f"https://github.com/{OWNER}/{SLUG}"
    assert repo.private is True
    assert repo.empty is True
    ref = repo.as_provider_ref()
    assert set(ref) == {"full_name", "node_id", "html_url", "private", "archived", "empty"}

    sent = t.sent("POST", "/user/repos")[0]
    assert sent.body == {"name": SLUG, "private": True, "auto_init": False}


def test_a_create_is_sent_with_retries_switched_off() -> None:
    """The at_most_once property, asserted at the layer that decides it.

    A reset arriving after the server accepted the body leaves a repository this
    process knows nothing about. Retrying makes a second one.
    """
    t = _fake()
    repos.create(t, OWNER, SLUG, owner_kind="user")
    assert t.sent("POST", "/user/repos")[0].retry_safe is False


def test_anything_other_than_private_and_empty_is_refused_before_a_request() -> None:
    """Harm reduction is not a default to be overridden casually."""
    for kwargs in ({"private": False}, {"auto_init": True}, {"private": False, "auto_init": True}):
        t = _fake()
        with pytest.raises(ValueError, match="delete_repo"):
            repos.create(t, OWNER, SLUG, owner_kind="user", **kwargs)  # type: ignore[arg-type]
        assert t.calls == []


def test_a_repo_that_comes_back_public_is_not_reported_as_a_success() -> None:
    """An organisation policy can override what we asked for. The flag is the property.

    The repository exists, so the error carries it: a caller must not treat this
    as success and must not lose track of it either.
    """
    t = _fake()
    public = t._repo_body(OWNER, SLUG, private=False)  # noqa: SLF001 - scripting the fake
    t.script["POST /user/repos"] = [Response(201, t.headers(), public)]
    with pytest.raises(repos.NotAsRequested) as caught:
        repos.create(t, OWNER, SLUG, owner_kind="user")
    assert caught.value.repo.full_name == f"{OWNER}/{SLUG}"
    assert "PUBLIC" in caught.value.reason


def test_a_second_create_is_the_only_reliable_collision_signal() -> None:
    t = _fake()
    repos.create(t, OWNER, SLUG, owner_kind="user")
    with pytest.raises(Unprocessable) as caught:
        repos.create(t, OWNER, SLUG, owner_kind="user")
    assert repos.name_already_exists(caught.value)
    # The reason is in errors[], not in message: a check against message alone
    # would silently never fire.
    assert "name already exists" not in caught.value.message.lower()


def test_a_422_about_something_else_is_not_a_collision() -> None:
    other = Unprocessable(422, "Validation Failed", errors=({"message": "name is too long"},))
    assert not repos.name_already_exists(other)
    assert not repos.name_already_exists(NotFound(404, "Not Found"))


def test_an_org_and_a_personal_repo_are_two_different_endpoints() -> None:
    t = _fake()
    repos.create(t, "some-org", SLUG, owner_kind="org")
    assert t.sent("POST", "/orgs/some-org/repos")

    t = _fake()
    repos.create(t, OWNER, SLUG)  # owner_kind="auto"
    assert [(c.method, c.path) for c in t.calls] == [
        ("GET", "/user"),
        ("POST", "/user/repos"),
    ]

    t = _fake()
    repos.create(t, "some-org", SLUG)
    assert t.sent("POST", "/orgs/some-org/repos")


def test_naming_the_owner_kind_saves_the_lookup() -> None:
    t = _fake()
    repos.create(t, OWNER, SLUG, owner_kind="user")
    assert t.sent("GET", "/user") == []


def test_a_description_is_sent_only_when_there_is_one() -> None:
    t = _fake()
    repos.create(t, OWNER, SLUG, owner_kind="user", description="what the voice said")
    assert t.sent("POST", "/user/repos")[0].body["description"] == "what the voice said"


# ───────────────────────────── the collision check ─────────────────────────────


def test_exists_is_one_read_that_changes_nothing() -> None:
    t = _fake()
    assert repos.exists(t, OWNER, SLUG) is False
    t.add_repo(f"{OWNER}/{SLUG}")
    assert repos.exists(t, OWNER, SLUG) is True
    assert t.writes == []


def test_get_returns_none_for_both_meanings_of_404() -> None:
    """A private repo the token cannot see answers 404, same as one that is absent."""
    t = _fake()
    assert repos.get(t, OWNER, "never-existed") is None


def test_a_response_missing_the_node_id_is_refused_rather_than_stored() -> None:
    """An effect row without it cannot find the repo again after a rename."""
    t = _fake()
    body = t._repo_body(OWNER, SLUG, private=True)  # noqa: SLF001 - scripting the fake
    del body["node_id"]
    t.script[f"GET /repos/{OWNER}/{SLUG}"] = [Response(200, t.headers(), body)]
    with pytest.raises(GithubError, match="no node_id"):
        repos.get(t, OWNER, SLUG)


def test_a_body_that_is_not_a_repository_object_is_refused() -> None:
    t = _fake()
    t.script[f"GET /repos/{OWNER}/{SLUG}"] = [Response(200, t.headers(), ["not", "a", "repo"])]
    with pytest.raises(GithubError, match="not a repository object"):
        repos.get(t, OWNER, SLUG)


# ───────────────────────────── the compensations ─────────────────────────────


def test_each_compensation_sends_exactly_the_field_it_is_named_after() -> None:
    t = _fake()
    t.add_repo(f"{OWNER}/{SLUG}")

    repos.rename(t, OWNER, SLUG, abandoned_name(SLUG))
    assert t.sent("PATCH")[-1].body == {"name": abandoned_name(SLUG)}

    repos.set_private(t, OWNER, abandoned_name(SLUG))
    assert t.sent("PATCH")[-1].body == {"private": True}

    repos.archive(t, OWNER, abandoned_name(SLUG))
    assert t.sent("PATCH")[-1].body == {"archived": True}
    assert all(c.retry_safe is False for c in t.sent("PATCH"))


def test_the_compensation_in_its_documented_order_works_end_to_end() -> None:
    t = _fake()
    t.add_repo(f"{OWNER}/{SLUG}")
    current = SLUG
    for op in repos.COMPENSATION_ORDER:
        if op == "rename":
            current = abandoned_name(SLUG)
            repo = repos.rename(t, OWNER, SLUG, current)
        elif op == "set_private":
            repo = repos.set_private(t, OWNER, current)
        else:
            repo = repos.archive(t, OWNER, current)
    assert repo.full_name == f"{OWNER}/{abandoned_name(SLUG)}"
    assert repo.private is True
    assert repo.archived is True


def test_archiving_first_breaks_the_other_two_compensations() -> None:
    """THE reason the order exists. An archived repository is READ-ONLY.

    If this ever stops being true the order can be simplified; until then, a
    compensation that archives first performs one third of what it promised and
    reports two 403s that look like a token problem.
    """
    t = _fake()
    t.add_repo(f"{OWNER}/{SLUG}")
    repos.archive(t, OWNER, SLUG)

    with pytest.raises(Forbidden) as caught:
        repos.rename(t, OWNER, SLUG, abandoned_name(SLUG))
    assert repos.archived_read_only(caught.value)

    with pytest.raises(Forbidden) as caught:
        repos.set_private(t, OWNER, SLUG)
    assert repos.archived_read_only(caught.value)

    # And the misdiagnosis this guards against: it is NOT a scope problem.
    missing_scope = Forbidden(403, "Resource not accessible by personal access token")
    assert not repos.archived_read_only(missing_scope)
    assert repos.COMPENSATION_ORDER[-1] == "archive"


def test_unarchiving_is_the_way_back_out_of_that_corner() -> None:
    t = _fake()
    t.add_repo(f"{OWNER}/{SLUG}", archived=True)
    assert repos.archive(t, OWNER, SLUG, archived=False).archived is False
    assert repos.rename(t, OWNER, SLUG, abandoned_name(SLUG)).name == abandoned_name(SLUG)


def test_a_token_without_the_repo_scope_is_refused_by_the_api_not_by_us() -> None:
    """We do not pre-judge: the capability matrix advises, the API decides."""
    t = FakeTransport(login=OWNER, scopes=("public_repo",))
    t.add_repo(f"{OWNER}/{SLUG}")
    with pytest.raises(Forbidden) as caught:
        repos.archive(t, OWNER, SLUG)
    assert caught.value.accepted_scopes == ("repo",)


# ───────────────────────────── names ─────────────────────────────


def test_a_name_that_would_not_survive_a_url_is_refused_not_encoded() -> None:
    for bad in ("comment watcher", "../../etc/passwd", "a/b", "-leading", "", "x" * 101, "yorum?"):
        assert not repos.valid_slug(bad), bad
        with pytest.raises(ValueError, match="usable repository name"):
            repos.check_slug(bad)


def test_the_names_this_stage_actually_produces_are_accepted() -> None:
    # What the Turkish slugger upstream is expected to hand over (ö->o, ş->s, ı->i).
    for good in ("comment-watcher", "yorum-izleyici", abandoned_name("comment-watcher"), "a"):
        assert repos.valid_slug(good), good
        assert repos.check_slug(good) == good


def test_an_owner_that_is_not_a_login_is_refused() -> None:
    t = _fake()
    for bad in ("", "a/b", "../x", "owner name"):
        with pytest.raises(ValueError, match="owner login"):
            repos.exists(t, bad, SLUG)
    assert t.calls == []


def test_a_repo_is_comparable_and_its_repr_says_what_matters() -> None:
    t = _fake()
    one = repos.create(t, OWNER, SLUG, owner_kind="user")
    again = repos.get(t, OWNER, SLUG)
    assert again is not None
    assert one != again  # `empty` is known at create time and unknowable on a read
    assert one.as_provider_ref()["node_id"] == again.as_provider_ref()["node_id"]
    assert "private=True" in repr(one) and SLUG in repr(one)
    assert one != "not a repo"
