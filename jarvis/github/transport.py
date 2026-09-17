"""The REST API as a seam: one protocol, an HTTP client, and a fake.

Four facts shape this module, and each one is a bug somebody has shipped.

*A 403 is two completely different events.* GitHub answers "you may not do this"
and "you have used your hour's requests" with the same status code, and the only
way to tell them apart is the rate-limit headers and the body's own wording. Get
it wrong and Jarvis says "you don't have permission to do that" when the truth is
"wait forty seconds" — which sends the user to the token settings page to fix
something that is not broken. :class:`RateLimited` is therefore its own type, and
:func:`is_rate_limit` is the one place the distinction is decided.

*The token must never reach a message, a log line or a traceback.* GitHub takes
it in an ``Authorization`` header rather than the URL, which removes the Telegram
footgun, but a dataclass ``repr`` puts every field in whatever prints it — so
:class:`HttpTransport` defines its own ``__repr__`` and the token is not in it.
The activity log redacts literal keyring values at write time
(:class:`jarvis.bus.Redactor`); this module is what makes that belt rather than
braces.

*Rate-limit headers arrive on EVERY response*, including the successful ones and
including the errors. That is what makes the capability matrix in
:mod:`jarvis.github.scopes` readable without creating anything, and it is why
:class:`Response` keeps the headers rather than returning a bare body.

*Some requests must never be retried blind.* ``POST /user/repos`` has no
idempotency key: a connection reset after the server accepted the body means the
repo exists, and a blind retry answers 422 at best and makes a second repo at
worst. Only GET and HEAD are retried here. Everything else raises, and the
durable retry decision belongs to ``outbox`` — the table whose ``at_most_once``
column exists for exactly this class of operation.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import sleep as _sleep
from typing import Any, Literal, Protocol

__all__ = [
    "ACCEPT",
    "API_ROOT",
    "API_VERSION",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TIMEOUT_S",
    "IDEMPOTENT_METHODS",
    "MAX_WAIT_S",
    "TOKEN_PREFIXES",
    "USER_AGENT",
    "Call",
    "FakeTransport",
    "Forbidden",
    "GithubError",
    "HttpTransport",
    "NotFound",
    "RateLimit",
    "RateLimited",
    "Response",
    "TokenKind",
    "Transport",
    "TransportError",
    "Unauthorized",
    "Unprocessable",
    "is_rate_limit",
    "split_scopes",
    "token_kind",
]

API_ROOT = "https://api.github.com"

#: Pinned, because an unpinned API version is a silent behaviour change on
#: somebody else's release schedule.
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"
USER_AGENT = "jarvis"

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_ATTEMPTS = 3

#: Honour a rate-limit wait up to here and then give up. A call that sleeps for
#: the rest of the hour inside one request is indistinguishable from a hang, and
#: the user is standing at the desk waiting for an answer.
MAX_WAIT_S = 60.0

#: The only methods retried automatically. See the module docstring: everything
#: else is the ``outbox``'s decision, once, with a durable row behind it.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})

TokenKind = Literal["classic", "fine_grained", "oauth", "app", "app_user", "unknown"]

#: Prefix -> what kind of credential it is. GitHub's token formats are
#: documented and stable, and the kind decides how much the scope header can be
#: trusted (see :mod:`jarvis.github.scopes`). A legacy 40-hex classic PAT has no
#: prefix at all and correctly comes back "unknown" — the header settles it.
TOKEN_PREFIXES: tuple[tuple[str, TokenKind], ...] = (
    ("github_pat_", "fine_grained"),
    ("ghp_", "classic"),
    ("gho_", "oauth"),
    ("ghu_", "app_user"),
    ("ghs_", "app"),
)


def token_kind(token: str) -> TokenKind:
    """Classify a credential by its prefix, WITHOUT revealing any of it.

    Returns one of a fixed set of words, so this is safe to print, log and put
    in an event payload. "unknown" is a real answer: a legacy classic PAT is 40
    hex characters with no prefix, and guessing from length would be a fact
    invented rather than measured.
    """
    for prefix, kind in TOKEN_PREFIXES:
        if token.startswith(prefix):
            return kind
    return "unknown"


# ───────────────────────────── errors ─────────────────────────────


class TransportError(RuntimeError):
    """The request never produced an API answer: DNS, TLS, timeout, reset.

    Never carries a URL, because a URL built here is built next to a token and
    the next person to edit the f-string will not be thinking about that.
    """


class GithubError(RuntimeError):
    """The API answered, and the answer was an error.

    Its own type because the caller's response differs: a transport failure may
    be worth repeating, whereas 422 "name already exists on this account" is a
    fact about the world that retrying cannot change — it means the collision
    loop should ask the user for another name.

    ``headers`` is kept because the interesting half of a GitHub error is in
    them: the rate-limit budget, and ``x-accepted-oauth-scopes`` naming what the
    endpoint wanted. Response headers hold no credential.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        method: str = "",
        path: str = "",
        documentation_url: str | None = None,
        errors: tuple[Any, ...] = (),
        headers: Mapping[str, str] | None = None,
    ) -> None:
        where = f"{method} {path}: " if method or path else ""
        super().__init__(f"{where}{status} {message}")
        self.status = status
        self.message = message
        self.method = method
        self.path = path
        self.documentation_url = documentation_url
        self.errors = errors
        self.headers: dict[str, str] = dict(headers or {})

    @property
    def rate(self) -> RateLimit:
        return RateLimit.from_headers(self.headers)

    @property
    def accepted_scopes(self) -> tuple[str, ...]:
        """Scopes the refused endpoint said it accepts, if it said.

        Only meaningful on a :class:`Forbidden`, and then it is the API telling
        us which scope is missing — the one piece of capability information that
        a refusal hands over for free.
        """
        return split_scopes(self.headers.get("x-accepted-oauth-scopes"))


class Unauthorized(GithubError):
    """401. The credential is wrong, revoked or expired — not a scope problem."""


class Forbidden(GithubError):
    """403 that is really about permission: a missing scope, or a read-only repo.

    Distinct from :class:`RateLimited`, which shares the status code and means
    the opposite thing about what the user should do next.
    """


class NotFound(GithubError):
    """404. On a repo read this is ambiguous by design.

    GitHub answers 404 rather than 403 for a private repository the token cannot
    see, so "no such repo" and "a repo you may not look at" are the same answer.
    :func:`jarvis.github.repos.exists` says so rather than pretending otherwise.
    """


class Unprocessable(GithubError):
    """422. The request was understood and refused — e.g. the name is taken."""


class RateLimited(GithubError):
    """403 or 429 that means WAIT, not "you may not".

    ``retry_after_s`` is an instruction, not a hint: it comes from ``retry-after``
    when the secondary limiter sent one, and otherwise from ``x-ratelimit-reset``
    minus now. It is what lets the spoken line be "give me a minute" instead of
    "check your token".
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        retry_after_s: float,
        **kw: Any,
    ) -> None:
        super().__init__(status, message, **kw)
        self.retry_after_s = max(0.0, float(retry_after_s))

    @property
    def resource(self) -> str | None:
        """Which bucket ran out: ``core``, ``search``, ``graphql``, …

        Search is its own 30/minute bucket, so a search that is limited says
        nothing about whether a repo can be created right now.
        """
        return self.headers.get("x-ratelimit-resource")


# ───────────────────────────── the wire ─────────────────────────────


@dataclass(frozen=True, slots=True)
class RateLimit:
    """The budget, as the last response reported it. Absent fields stay None."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: int | None = None
    used: int | None = None
    resource: str | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimit:
        return cls(
            limit=_int_or_none(headers.get("x-ratelimit-limit")),
            remaining=_int_or_none(headers.get("x-ratelimit-remaining")),
            reset_at=_int_or_none(headers.get("x-ratelimit-reset")),
            used=_int_or_none(headers.get("x-ratelimit-used")),
            resource=headers.get("x-ratelimit-resource"),
        )

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def seconds_until_reset(self, *, now_epoch: float | None = None) -> float | None:
        """How long until the bucket refills, or None if the header was absent."""
        if self.reset_at is None:
            return None
        return max(0.0, self.reset_at - (now_epoch if now_epoch is not None else time.time()))


@dataclass(frozen=True, slots=True)
class Response:
    """One answer: status, headers with lowercased keys, and the parsed body.

    The headers are half the point of this class. Every authenticated response
    carries the granted scopes and the rate-limit budget, which is how the
    capability matrix is read without creating, renaming or deleting anything.
    """

    status: int
    headers: Mapping[str, str]
    body: Any = None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def rate(self) -> RateLimit:
        return RateLimit.from_headers(self.headers)


@dataclass(frozen=True, slots=True)
class Call:
    """One recorded outbound request. The fake's entire assertion surface.

    ``retry_safe`` is recorded because it is a correctness property rather than a
    tuning knob: a test has to be able to prove that a repo creation was sent
    with retries switched OFF.
    """

    method: str
    path: str
    body: dict[str, Any] | None = None
    retry_safe: bool | None = None


class Transport(Protocol):
    """Everything this package needs from GitHub, and nothing else.

    One method, because unlike the Bot API the REST API has exactly one encoding:
    a JSON body on a path. ``token_kind`` is here rather than a token accessor on
    purpose — :mod:`jarvis.github.scopes` needs to know whether an absent scope
    header means "fine-grained, cannot tell" or "classic with no scopes", and
    that question must be answerable without any module ever holding the
    credential.
    """

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        retry_safe: bool | None = None,
    ) -> Response:
        """Perform one request and return its :class:`Response`.

        Raises :class:`GithubError` (or a subclass) when the API answered with an
        error, and :class:`TransportError` when there was no answer at all.
        """

    def token_kind(self) -> TokenKind:
        """What KIND of credential this transport holds. Never the credential."""


# ───────────────────────────── the real one ─────────────────────────────


@dataclass(slots=True, repr=False)
class HttpTransport:
    """``urllib.request`` against api.github.com. No third-party dependency.

    ``sleep``, ``opener`` and ``now_epoch`` are injected so the retry ladder and
    the rate-limit arithmetic are testable without a network and without a test
    that really waits. They are constructor arguments rather than module globals
    because a probe and the daemon must be able to exist in one process without
    sharing state.

    ``repr`` is defined by hand: a dataclass ``repr`` prints every field, and one
    of these fields is a live credential.
    """

    token: str
    api_root: str = API_ROOT
    attempts: int = DEFAULT_ATTEMPTS
    timeout_s: float = DEFAULT_TIMEOUT_S
    user_agent: str = USER_AGENT
    max_wait_s: float = MAX_WAIT_S
    sleep: Callable[[float], None] = _sleep
    now_epoch: Callable[[], float] = time.time
    opener: Any = None

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("HttpTransport needs a token; FakeTransport needs none")
        if self.attempts < 1:
            raise ValueError("attempts is 1-based")
        if self.opener is None:
            self.opener = urllib.request.build_opener()

    def __repr__(self) -> str:
        """Deliberately token-free: this string ends up in logs and tracebacks."""
        return (
            f"HttpTransport(api_root={self.api_root!r}, kind={self.token_kind()!r}, "
            f"attempts={self.attempts}, timeout_s={self.timeout_s})"
        )

    def token_kind(self) -> TokenKind:
        return token_kind(self.token)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        retry_safe: bool | None = None,
    ) -> Response:
        verb = method.upper()
        safe = _retry_safe(verb, retry_safe)
        budget = self.timeout_s if timeout is None else timeout
        payload = None if body is None else json.dumps(dict(body), ensure_ascii=False).encode()
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                return self._once(verb, path, payload, budget)
            except RateLimited as e:
                # The server has said how long to wait. Waiting is cheaper than
                # being throttled harder, but only for a request that is safe to
                # repeat at all: a create is the outbox's problem, not ours.
                if not safe or attempt == self.attempts or e.retry_after_s > self.max_wait_s:
                    raise
                last = e
                self.sleep(e.retry_after_s)
            except TransportError as e:
                if not safe or attempt == self.attempts:
                    raise
                last = e
                self.sleep(min(2.0 ** (attempt - 1), self.max_wait_s))
        # Not reachable: the loop either returns or raises on its last attempt.
        # Kept because falling out of a `for` returns None, and a None where a
        # Response is expected fails a long way from the cause.
        raise TransportError(f"{verb} {path}: gave up after {self.attempts} attempts") from last

    def _once(self, verb: str, path: str, payload: bytes | None, timeout: float) -> Response:
        req = urllib.request.Request(self._url(path), data=payload, method=verb)
        req.add_header("Accept", ACCEPT)
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("User-Agent", self.user_agent)
        # The credential goes in a header and is added LAST, so nothing below can
        # accidentally log the request object with it half-built and look safe.
        req.add_header("Authorization", f"Bearer {self.token}")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                headers = _norm(resp.headers.items())
                raw = resp.read()
                status = int(getattr(resp, "status", 200) or 200)
                return Response(status, headers, _parse(raw, verb, path))
        except urllib.error.HTTPError as e:
            raise _error_from(e, verb, path, self.now_epoch()) from None
        except (urllib.error.URLError, OSError) as e:
            # The type name only. An OSError's own message can contain the host
            # and, in a proxy's case, whatever the proxy chose to echo back.
            raise TransportError(f"{verb} {path}: {type(e).__name__}") from e

    def _url(self, path: str) -> str:
        return f"{self.api_root.rstrip('/')}/{path.lstrip('/')}"


def _error_from(e: urllib.error.HTTPError, verb: str, path: str, now: float) -> GithubError:
    """Turn an HTTPError into the narrowest type that fits, body included.

    GitHub explains every refusal in a JSON body, and throwing that away turns
    "name already exists on this account" into "HTTP 422" — which is the
    difference between a spoken rename loop and a shrug.
    """
    headers = _norm(e.headers.items() if e.headers else [])
    body: Any = None
    try:
        raw = e.read()
    except Exception:  # noqa: BLE001 - a body we cannot read must not mask the status
        raw = b""
    if raw:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body = None
    message = ""
    documentation_url = None
    errors: tuple[Any, ...] = ()
    if isinstance(body, dict):
        message = str(body.get("message") or "")
        documentation_url = body.get("documentation_url")
        raw_errors = body.get("errors")
        errors = tuple(raw_errors) if isinstance(raw_errors, list) else ()
    status = int(e.code)
    kw: dict[str, Any] = {
        "method": verb,
        "path": path,
        "documentation_url": documentation_url,
        "errors": errors,
        "headers": headers,
    }
    if not message:
        message = e.reason if isinstance(e.reason, str) else "no message"
    if is_rate_limit(status, headers, message):
        return RateLimited(status, message, retry_after_s=_wait_for(headers, now_epoch=now), **kw)
    if status == 401:
        return Unauthorized(status, message, **kw)
    if status == 403:
        return Forbidden(status, message, **kw)
    if status == 404:
        return NotFound(status, message, **kw)
    if status == 422:
        return Unprocessable(status, message, **kw)
    return GithubError(status, message, **kw)


#: Wordings GitHub uses when the answer is "wait". The primary limiter says
#: "API rate limit exceeded"; the secondary one says "secondary rate limit" and
#: older builds said "abuse detection mechanism". None of them is a permission
#: problem, and all of them arrive as 403.
RATE_LIMIT_PHRASES: tuple[str, ...] = (
    "rate limit",
    "abuse detection",
    "too many requests",
)


def is_rate_limit(status: int, headers: Mapping[str, str], message: str) -> bool:
    """THE 403 fork: a budget problem, or a permission problem?

    Three independent signals, any of which settles it. Checked in this order
    because the body's own wording is the only one that is unambiguous, and
    ``x-ratelimit-remaining: 0`` is definitive in the other direction: with the
    bucket empty every request gets this answer regardless of its scopes, so
    telling the user to fix their token would be advice about the wrong problem.

    A genuine permission 403 carries rate-limit headers too, with ``remaining``
    well above zero — which is why the header's VALUE and not its presence is
    what is tested.
    """
    if status not in (403, 429):
        return False
    low = message.lower()
    if any(phrase in low for phrase in RATE_LIMIT_PHRASES):
        return True
    if headers.get("x-ratelimit-remaining") == "0":
        return True
    return status == 429 and "retry-after" in headers


def _wait_for(headers: Mapping[str, str], *, now_epoch: float) -> float:
    retry_after = _int_or_none(headers.get("retry-after"))
    if retry_after is not None:
        return float(retry_after)
    until = RateLimit.from_headers(headers).seconds_until_reset(now_epoch=now_epoch)
    # No usable header at all: a minute is long enough not to hammer and short
    # enough that the caller's own budget, not this number, decides to give up.
    return 60.0 if until is None else until


def _retry_safe(verb: str, retry_safe: bool | None) -> bool:
    """Whether this request may be repeated — and a REFUSAL, not a default.

    ``retry_safe=True`` on a ``POST``/``PATCH``/``PUT``/``DELETE`` is refused
    rather than honoured. ``POST /user/repos`` has no idempotency key, so a
    transport that repeated one on a caller's word could create the user's
    repository twice, and the only durable place that decision belongs is an
    ``outbox`` row with ``at_most_once=1``. Making it a ``ValueError`` means the
    property survives a future caller who passes the flag without reading why it
    is there.
    """
    if retry_safe and verb not in IDEMPOTENT_METHODS:
        raise ValueError(
            f"{verb} may not be marked retry_safe: it has no idempotency key, so a repeat can "
            "act twice. A second attempt is the outbox's decision (at_most_once=1), not this "
            "transport's."
        )
    return verb in IDEMPOTENT_METHODS if retry_safe is None else retry_safe


def _parse(raw: bytes, verb: str, path: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise TransportError(f"{verb} {path}: response was not JSON") from e


def _norm(items: Any) -> dict[str, str]:
    """Lowercase the keys once, here, so no caller has to guess the casing."""
    return {str(k).lower(): str(v) for k, v in items}


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def split_scopes(value: str | None) -> tuple[str, ...]:
    """A comma-separated header value as a tuple. ``None`` and "" both give ()."""
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


# ───────────────────────────── the fake ─────────────────────────────


@dataclass(slots=True)
class FakeTransport:
    """Records what was sent, replays what was scripted. Every test runs on this.

    It models three behaviours of the real API that matter to this stage, so a
    test can exercise them without a token:

    * the scope headers, including the two ways they can be USELESS — absent (a
      fine-grained PAT) and present but empty (a classic token with no scopes);
    * 422 ``name already exists on this account`` for a colliding create, which
      is the only reliable collision signal, since a private repo the token
      cannot see answers 404 to a read;
    * **an archived repository is read-only.** A ``PATCH`` to a repo already
      archived here answers 403 the way GitHub does, which is what makes the
      compensation ORDER in :mod:`jarvis.github.repos` testable rather than
      merely commented.

    ``script`` overrides all of it, keyed ``"METHOD /path"``, holding
    :class:`Response` objects, exceptions to raise, or plain dicts (taken as a
    200 body).
    """

    login: str = "octocat"
    scopes: tuple[str, ...] | None = ("repo",)
    kind: TokenKind = "classic"
    repos: dict[str, dict[str, Any]] = field(default_factory=dict)
    script: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    rate_remaining: int = 4999
    rate_limit: int = 5000
    rate_reset_at: int = 1_800_000_000
    next_node_id: int = 1

    def token_kind(self) -> TokenKind:
        return self.kind

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        retry_safe: bool | None = None,
    ) -> Response:
        verb = method.upper()
        # Refused here as well as in HttpTransport, so a test cannot pass on a
        # flag that would raise against the real API.
        _retry_safe(verb, retry_safe)
        payload = None if body is None else dict(body)
        self.calls.append(Call(verb, path, payload, retry_safe))
        queued = self.script.get(f"{verb} {path}")
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, Response):
                return item
            return Response(200, self.headers(), item)
        return self._default(verb, path, payload)

    # -- what a test asks it afterwards ------------------------------------

    def sent(self, method: str, path: str | None = None) -> list[Call]:
        verb = method.upper()
        return [c for c in self.calls if c.method == verb and (path is None or c.path == path)]

    @property
    def writes(self) -> list[Call]:
        """Every call that could change anything. Read-only means this is empty."""
        return [c for c in self.calls if c.method not in IDEMPOTENT_METHODS]

    def add_repo(
        self,
        full_name: str,
        *,
        private: bool = True,
        archived: bool = False,
    ) -> dict[str, Any]:
        owner, _, name = full_name.partition("/")
        repo = self._repo_body(owner, name, private=private, archived=archived)
        self.repos[full_name.lower()] = repo
        return repo

    def headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The headers every authenticated response carries, scopes included."""
        out = {
            "x-ratelimit-limit": str(self.rate_limit),
            "x-ratelimit-remaining": str(self.rate_remaining),
            "x-ratelimit-reset": str(self.rate_reset_at),
            "x-ratelimit-used": str(self.rate_limit - self.rate_remaining),
            "x-ratelimit-resource": "core",
        }
        if self.scopes is not None:
            out["x-oauth-scopes"] = ", ".join(self.scopes)
        out.update({k.lower(): v for k, v in (extra or {}).items()})
        return out

    # -- internals ---------------------------------------------------------

    def _default(self, verb: str, path: str, body: dict[str, Any] | None) -> Response:
        if verb == "GET" and path == "/user":
            return Response(200, self.headers(), {"login": self.login, "type": "User"})
        if path.startswith("/repos/"):
            return self._repo_endpoint(verb, path, body)
        if verb == "POST" and (path == "/user/repos" or path.startswith("/orgs/")):
            owner = self.login if path == "/user/repos" else path.split("/")[2]
            return self._create(owner, dict(body or {}))
        raise AssertionError(f"FakeTransport has no default for {verb} {path}")

    def _repo_endpoint(self, verb: str, path: str, body: dict[str, Any] | None) -> Response:
        parts = path.strip("/").split("/")
        full_name = f"{parts[1]}/{parts[2]}"
        repo = self.repos.get(full_name.lower())
        if repo is None:
            raise NotFound(404, "Not Found", method=verb, path=path, headers=self.headers())
        if verb == "GET":
            return Response(200, self.headers(), repo)
        if verb == "DELETE":
            if not self._may("delete_repo"):
                raise Forbidden(
                    403,
                    "Must have admin rights to Repository.",
                    method=verb,
                    path=path,
                    headers=self.headers({"x-accepted-oauth-scopes": "delete_repo"}),
                )
            del self.repos[full_name.lower()]
            return Response(204, self.headers(), None)
        if verb == "PATCH":
            return self._patch(verb, path, full_name, repo, dict(body or {}))
        raise AssertionError(f"FakeTransport has no default for {verb} {path}")

    def _patch(
        self,
        verb: str,
        path: str,
        full_name: str,
        repo: dict[str, Any],
        body: dict[str, Any],
    ) -> Response:
        if repo.get("archived") and body.get("archived") is not False:
            # The real refusal, wording included. Archiving is what makes every
            # other compensation stop working, so it has to happen LAST.
            raise Forbidden(
                403,
                "Repository was archived so is read-only.",
                method=verb,
                path=path,
                headers=self.headers(),
            )
        if not self._may("repo"):
            raise Forbidden(
                403,
                "Resource not accessible by personal access token",
                method=verb,
                path=path,
                headers=self.headers({"x-accepted-oauth-scopes": "repo"}),
            )
        owner = full_name.split("/")[0]
        name = str(body.get("name") or repo["name"])
        updated = self._repo_body(
            owner,
            name,
            private=bool(body.get("private", repo["private"])),
            archived=bool(body.get("archived", repo.get("archived", False))),
            node_id=repo["node_id"],
        )
        del self.repos[full_name.lower()]
        self.repos[updated["full_name"].lower()] = updated
        return Response(200, self.headers(), updated)

    def _create(self, owner: str, body: dict[str, Any]) -> Response:
        name = str(body.get("name") or "")
        full_name = f"{owner}/{name}"
        if full_name.lower() in self.repos:
            raise Unprocessable(
                422,
                "Repository creation failed.",
                method="POST",
                path="/user/repos",
                errors=({"message": "name already exists on this account"},),
                headers=self.headers(),
            )
        if not self._may("repo"):
            raise Forbidden(
                403,
                "Resource not accessible by personal access token",
                method="POST",
                path="/user/repos",
                headers=self.headers({"x-accepted-oauth-scopes": "repo"}),
            )
        repo = self._repo_body(owner, name, private=bool(body.get("private", False)))
        self.repos[full_name.lower()] = repo
        return Response(201, self.headers(), repo)

    def _may(self, scope: str) -> bool:
        """A fake token with unreadable scopes can still DO things.

        ``scopes=None`` models a fine-grained PAT: the header says nothing, and
        the operations nonetheless work. That combination — capability "unknown",
        behaviour fine — is the case the tri-state exists for, so the fake has to
        be able to produce it.
        """
        return self.scopes is None or scope in self.scopes

    def _repo_body(
        self,
        owner: str,
        name: str,
        *,
        private: bool,
        archived: bool = False,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        if node_id is None:
            node_id = f"R_kgDO{self.next_node_id:06d}"
            self.next_node_id += 1
        return {
            "name": name,
            "full_name": f"{owner}/{name}",
            "node_id": node_id,
            "html_url": f"https://github.com/{owner}/{name}",
            "owner": {"login": owner},
            "private": private,
            "archived": archived,
            "default_branch": "main",
            "size": 0,
        }
