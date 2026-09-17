"""The REST seam, tested where GitHub's answers mean two different things.

Nothing here opens a socket. api.github.com is reachable from this machine and
there are real credentials in the environment, which is precisely why every test
in this package runs against :class:`FakeTransport` or a fake opener: creating a
repository is irreversible and visible, and it would be on somebody's real
account.

What IS tested is every place the REST API's shape differs from what a naive
client would assume — a 403 that means "wait" rather than "no", a JSON
explanation inside a 4xx, a body whose real error is in ``errors[]`` — and the
one property no test can be allowed to lose: the token does not appear in
anything a human or a log file ever sees.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from jarvis.github.transport import (
    API_VERSION,
    FakeTransport,
    Forbidden,
    GithubError,
    HttpTransport,
    NotFound,
    RateLimit,
    RateLimited,
    Response,
    TransportError,
    Unauthorized,
    Unprocessable,
    is_rate_limit,
    split_scopes,
    token_kind,
)

TOKEN = "ghp_THIS-IS-NOT-A-REAL-TOKEN-0123456789"
NOW = 1_700_000_000.0

RATE_OK = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4999",
    "x-ratelimit-reset": str(int(NOW) + 3600),
    "x-ratelimit-resource": "core",
    "x-oauth-scopes": "repo, gist",
}


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Opener:
    """Stands in for ``urllib.request.OpenerDirector``. Never touches a network."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.requests: list[Any] = []

    def open(self, req: Any, timeout: float | None = None) -> _Resp:
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, _Resp):
            return answer
        return _Resp(json.dumps(answer).encode(), 200, dict(RATE_OK))


def _transport(answers: list[Any], slept: list[float] | None = None) -> HttpTransport:
    return HttpTransport(
        TOKEN,
        opener=_Opener(answers),
        sleep=(slept if slept is not None else []).append,
        now_epoch=lambda: NOW,
    )


def _http_error(
    status: int,
    body: Any,
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode() if body is not None else b""
    return urllib.error.HTTPError(
        "https://api.github.invalid/x",
        status,
        "Error",
        dict(RATE_OK) if headers is None else headers,  # type: ignore[arg-type]
        io.BytesIO(raw),
    )


# ───────────────────── the 403 fork, which is the whole point ─────────────────────


def test_a_403_carrying_a_rate_limit_body_is_a_wait_not_a_refusal() -> None:
    """Conflating these makes Jarvis send the user to fix a token that is fine."""
    headers = {**RATE_OK, "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 40)}
    t = _transport([_http_error(403, {"message": "API rate limit exceeded for user 1."}, headers)])
    with pytest.raises(RateLimited) as caught:
        t.request("POST", "/user/repos", body={"name": "x"})
    assert caught.value.retry_after_s == pytest.approx(40.0)
    assert caught.value.resource == "core"
    assert not isinstance(caught.value, Forbidden)


def test_a_403_that_really_is_permission_says_which_scope_was_wanted() -> None:
    headers = {**RATE_OK, "x-accepted-oauth-scopes": "delete_repo"}
    t = _transport([_http_error(403, {"message": "Must have admin rights."}, headers)])
    with pytest.raises(Forbidden) as caught:
        t.request("DELETE", "/repos/o/r")
    assert not isinstance(caught.value, RateLimited)
    assert caught.value.accepted_scopes == ("delete_repo",)
    # And the budget was never the problem, which is how we know the distinction
    # was made on the right evidence.
    assert caught.value.rate.remaining == 4999


def test_an_empty_budget_is_a_wait_whatever_the_message_says() -> None:
    """With remaining=0 every request gets this answer, scopes or no scopes."""
    headers = {**RATE_OK, "x-ratelimit-remaining": "0"}
    assert is_rate_limit(403, headers, "Resource not accessible by personal access token")
    assert not is_rate_limit(403, RATE_OK, "Resource not accessible by personal access token")
    assert not is_rate_limit(404, headers, "Not Found")


def test_the_secondary_limiter_is_recognised_by_its_own_wording() -> None:
    for message in (
        "You have exceeded a secondary rate limit.",
        "You have triggered an abuse detection mechanism.",
    ):
        assert is_rate_limit(403, RATE_OK, message), message


def test_a_429_with_retry_after_is_obeyed_to_the_second() -> None:
    slept: list[float] = []
    headers = {**RATE_OK, "retry-after": "17"}
    opener = _Opener([_http_error(429, {"message": "Too many requests"}, headers), {"login": "o"}])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append, now_epoch=lambda: NOW)
    assert t.request("GET", "/user").body == {"login": "o"}
    assert slept == [17.0]


def test_a_rate_limited_write_is_never_slept_through_here() -> None:
    """A create is the outbox's decision, once, with a durable row behind it."""
    slept: list[float] = []
    headers = {**RATE_OK, "retry-after": "5"}
    opener = _Opener([_http_error(429, {"message": "Too many requests"}, headers)])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append, now_epoch=lambda: NOW)
    with pytest.raises(RateLimited):
        t.request("POST", "/user/repos", body={"name": "x"})
    assert slept == []
    assert len(opener.requests) == 1


def test_a_wait_longer_than_the_budget_is_raised_rather_than_slept() -> None:
    slept: list[float] = []
    headers = {**RATE_OK, "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3000)}
    t = _transport([_http_error(403, {"message": "API rate limit exceeded"}, headers)], slept)
    with pytest.raises(RateLimited) as caught:
        t.request("GET", "/user")
    assert caught.value.retry_after_s == pytest.approx(3000.0)
    assert slept == []


def test_a_rate_limit_with_no_usable_header_still_names_a_wait() -> None:
    """No retry-after and no reset: a made-up minute beats an unbounded wait."""
    slept: list[float] = []
    errors = [_http_error(403, {"message": "API rate limit exceeded"}, {}) for _ in range(3)]
    t = _transport(errors, slept)
    with pytest.raises(RateLimited) as caught:
        t.request("GET", "/user")
    assert caught.value.retry_after_s == 60.0
    assert slept == [60.0, 60.0]


# ───────────────────────────── the token ─────────────────────────────


def test_the_token_never_appears_in_an_error_message() -> None:
    """The equivalent of the Telegram test, for every failure path there is."""
    failures: list[Any] = [
        lambda: OSError("connection reset by peer"),
        lambda: urllib.error.URLError("no route"),
        lambda: _http_error(401, {"message": "Bad credentials"}),
        lambda: _Resp(b"<html>not json</html>", 200, dict(RATE_OK)),
    ]
    for failure in failures:
        t = _transport([failure(), failure(), failure()])
        with pytest.raises((TransportError, GithubError)) as caught:
            t.request("GET", "/user")
        exc = caught.value
        blob = " ".join(
            [str(exc), repr(exc), repr(exc.args), str(exc.__cause__), repr(exc.__cause__)]
        )
        assert TOKEN not in blob, failure


def test_the_token_is_not_in_the_transports_own_repr() -> None:
    """A dataclass repr prints every field, and whatever prints it keeps a copy."""
    t = _transport([])
    assert TOKEN not in repr(t)
    assert "classic" in repr(t)


def test_the_token_travels_in_a_header_and_not_in_the_url() -> None:
    opener = _Opener([{"login": "octocat"}])
    t = HttpTransport(TOKEN, opener=opener, now_epoch=lambda: NOW)
    t.request("GET", "/user")
    req = opener.requests[0]
    assert TOKEN not in req.full_url
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("X-github-api-version") == API_VERSION


def test_token_kind_classifies_without_revealing() -> None:
    assert token_kind("ghp_abc") == "classic"
    assert token_kind("github_pat_abc") == "fine_grained"
    assert token_kind("ghs_abc") == "app"
    # A legacy 40-hex classic PAT has no prefix, and guessing would be inventing.
    assert token_kind("a" * 40) == "unknown"


def test_a_transport_with_no_token_refuses_to_exist() -> None:
    with pytest.raises(ValueError, match="FakeTransport"):
        HttpTransport("")


# ───────────────────────────── shapes and retries ─────────────────────────────


def test_the_json_explanation_inside_a_4xx_is_read_rather_than_discarded() -> None:
    body = {
        "message": "Repository creation failed.",
        "errors": [{"resource": "Repository", "message": "name already exists on this account"}],
        "documentation_url": "https://docs.github.com/rest",
    }
    t = _transport([_http_error(422, body)])
    with pytest.raises(Unprocessable) as caught:
        t.request("POST", "/user/repos", body={"name": "x"})
    assert "Repository creation failed" in str(caught.value)
    assert "name already exists" in str(caught.value.errors[0])
    assert caught.value.documentation_url is not None


def test_each_status_gets_the_narrowest_type_that_fits() -> None:
    pairs = ((401, Unauthorized), (403, Forbidden), (404, NotFound), (422, Unprocessable))
    for status, kind in pairs:
        t = _transport([_http_error(status, {"message": "no"})])
        with pytest.raises(kind):
            t.request("GET", "/user")


def test_a_status_nobody_anticipated_is_still_a_github_error() -> None:
    t = _transport([_http_error(451, {"message": "Repository access blocked"})])
    with pytest.raises(GithubError) as caught:
        t.request("GET", "/repos/o/r")
    assert caught.value.status == 451


def test_a_read_is_retried_and_a_write_is_not() -> None:
    slept: list[float] = []
    opener = _Opener([OSError("reset"), {"login": "octocat"}])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append, now_epoch=lambda: NOW)
    assert t.request("GET", "/user").body == {"login": "octocat"}
    assert len(opener.requests) == 2

    opener = _Opener([OSError("reset after the server accepted the body")])
    t = HttpTransport(TOKEN, opener=opener, sleep=slept.append, now_epoch=lambda: NOW)
    with pytest.raises(TransportError):
        t.request("POST", "/user/repos", body={"name": "comment-watcher"})
    # The whole at_most_once argument in one assertion: a second POST here could
    # make a second repository nobody asked for.
    assert len(opener.requests) == 1


def test_a_write_cannot_be_talked_into_being_retried() -> None:
    """The property has to survive a caller who passes the flag without reading why.

    ``retry_safe=True`` on a write is the one way the at_most_once guarantee could
    be lost inside this transport, so it is a refusal rather than an option — and
    the fake refuses it too, or a test would pass where production raises.
    """
    opener = _Opener([OSError("reset")] * 3)
    t = HttpTransport(TOKEN, opener=opener, sleep=lambda s: None, now_epoch=lambda: NOW)
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(ValueError, match="may not be marked retry_safe"):
            t.request(verb, "/user/repos", body={"name": "x"}, retry_safe=True)
    assert opener.requests == []

    fake = FakeTransport()
    with pytest.raises(ValueError, match="may not be marked retry_safe"):
        fake.request("POST", "/user/repos", body={"name": "x"}, retry_safe=True)
    assert fake.calls == []
    # A read may still say so out loud, and a write may still say retry_safe=False.
    assert fake.request("GET", "/user", retry_safe=True).status == 200


def test_the_last_failure_is_what_surfaces_and_it_names_no_url() -> None:
    """A URL built here is built next to a token, so errors name the path only."""
    t = _transport([OSError("a"), OSError("b"), OSError("c")])
    with pytest.raises(TransportError) as caught:
        t.request("GET", "/user")
    assert str(caught.value) == "GET /user: OSError"
    assert "api.github.com" not in str(caught.value)


def test_a_body_that_is_not_json_is_a_transport_failure_not_a_result() -> None:
    t = _transport([_Resp(b"<html>502</html>", 200, dict(RATE_OK)) for _ in range(3)])
    with pytest.raises(TransportError, match="not JSON"):
        t.request("GET", "/user")


def test_an_empty_body_is_a_successful_none() -> None:
    """204 No Content is what a successful DELETE looks like."""
    t = _transport([_Resp(b"", 204, dict(RATE_OK))])
    resp = t.request("DELETE", "/repos/o/r")
    assert resp.status == 204
    assert resp.body is None


def test_headers_are_lowercased_once_so_no_caller_guesses_the_casing() -> None:
    t = _transport([_Resp(b"{}", 200, {"X-OAuth-Scopes": "repo", "X-RateLimit-Remaining": "12"})])
    resp = t.request("GET", "/user")
    assert resp.header("x-oauth-scopes") == "repo"
    assert resp.header("X-OAuth-Scopes") == "repo"
    assert resp.rate.remaining == 12


def test_the_rate_budget_reads_back_as_numbers_and_none() -> None:
    rate = RateLimit.from_headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "100"})
    assert rate.exhausted
    assert rate.seconds_until_reset(now_epoch=60.0) == 40.0
    assert rate.seconds_until_reset(now_epoch=1000.0) == 0.0
    assert RateLimit.from_headers({}).seconds_until_reset(now_epoch=1.0) is None
    assert RateLimit.from_headers({"x-ratelimit-limit": "junk"}).limit is None


def test_split_scopes_treats_absent_and_empty_alike_but_only_here() -> None:
    assert split_scopes(None) == ()
    assert split_scopes("") == ()
    assert split_scopes("repo, delete_repo") == ("repo", "delete_repo")


# ───────────────────────────── the fake ─────────────────────────────


def test_the_fake_records_every_call_and_separates_the_writes() -> None:
    t = FakeTransport()
    t.request("GET", "/user")
    t.request("POST", "/user/repos", body={"name": "comment-watcher", "private": True})
    assert [c.method for c in t.calls] == ["GET", "POST"]
    assert [c.path for c in t.writes] == ["/user/repos"]
    assert t.sent("POST", "/user/repos")[0].body == {"name": "comment-watcher", "private": True}


def test_the_fake_answers_a_second_create_the_way_github_does() -> None:
    t = FakeTransport()
    t.request("POST", "/user/repos", body={"name": "comment-watcher", "private": True})
    with pytest.raises(Unprocessable) as caught:
        t.request("POST", "/user/repos", body={"name": "comment-watcher", "private": True})
    assert "name already exists" in str(caught.value.errors[0])


def test_the_fake_refuses_to_delete_without_the_scope() -> None:
    t = FakeTransport(scopes=("repo",))
    t.add_repo("octocat/comment-watcher")
    with pytest.raises(Forbidden) as caught:
        t.request("DELETE", "/repos/octocat/comment-watcher")
    assert caught.value.accepted_scopes == ("delete_repo",)

    allowed = FakeTransport(scopes=("repo", "delete_repo"))
    allowed.add_repo("octocat/comment-watcher")
    assert allowed.request("DELETE", "/repos/octocat/comment-watcher").status == 204
    assert allowed.repos == {}


def test_the_fake_reproduces_the_read_only_archive_refusal() -> None:
    """The reason the compensation has an order at all."""
    t = FakeTransport()
    t.add_repo("octocat/comment-watcher", archived=True)
    with pytest.raises(Forbidden, match="read-only"):
        t.request("PATCH", "/repos/octocat/comment-watcher", body={"name": "zz-abandoned-x"})
    # Unarchiving is the one PATCH an archived repo accepts.
    assert t.request("PATCH", "/repos/octocat/comment-watcher", body={"archived": False}).status


def test_the_fake_can_be_scripted_over_its_defaults() -> None:
    t = FakeTransport(script={"GET /user": [{"login": "someone-else"}, NotFound(404, "gone")]})
    assert t.request("GET", "/user").body == {"login": "someone-else"}
    with pytest.raises(NotFound):
        t.request("GET", "/user")
    # Script exhausted: back to the default behaviour rather than a crash.
    assert t.request("GET", "/user").body["login"] == "octocat"


def test_the_fake_carries_the_headers_the_matrix_is_read_from() -> None:
    t = FakeTransport(scopes=("repo", "delete_repo"))
    resp = t.request("GET", "/user")
    assert resp.header("x-oauth-scopes") == "repo, delete_repo"
    assert resp.rate.remaining == 4999

    absent = FakeTransport(scopes=None, kind="fine_grained")
    assert absent.request("GET", "/user").header("x-oauth-scopes") is None


def test_a_response_is_not_a_place_to_put_a_credential() -> None:
    """Belt and braces: the only strings in a Response come from the wire."""
    resp = Response(200, {"x-oauth-scopes": "repo"}, {"login": "octocat"})
    assert TOKEN not in repr(resp)
