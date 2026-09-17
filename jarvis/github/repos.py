"""Create a repository, and the three things that can be done about it afterwards.

``create`` is the one call in this system that cannot be taken back. The token
deliberately has no ``delete_repo`` scope, so the design does not try to reverse
the harm — it engineers the harm down to nothing:

* ``private=True`` — nobody but the owner ever sees it;
* ``auto_init=False`` — it has no commits, so there is nothing in it to regret.

An unwanted repository that is private and empty costs a name. That is the whole
safety property, it is the reason :func:`jarvis.effects.github_repo_create_effect`
REFUSES to record a creation that is not both, and it is why the two flags are
arguments here rather than assumptions: the call site states the property out
loud, and this function checks it. Any other combination raises, because there is
no confirmation flow in this system for creating something that costs more than
a name.

THE COMPENSATIONS HAVE AN ORDER, AND IT IS NOT THE ORDER THEY ARE SPOKEN IN.
Archiving a repository makes it READ-ONLY, and a read-only repository cannot be
renamed or have its visibility changed — GitHub answers 403 "Repository was
archived so is read-only." So :data:`COMPENSATION_ORDER` archives LAST, and
:class:`jarvis.github.transport.FakeTransport` reproduces that refusal so the
ordering is covered by a test rather than by this paragraph. (Confirm against the
live API before stage 4 closes; it is listed as an open question in the stage
report.)
"""

from __future__ import annotations

import re
from typing import Any, Literal

from jarvis.github.transport import (
    GithubError,
    NotFound,
    Response,
    Transport,
    Unprocessable,
)

__all__ = [
    "ARCHIVED_READ_ONLY",
    "COMPENSATION_ORDER",
    "DEFAULT_AUTO_INIT",
    "DEFAULT_PRIVATE",
    "MAX_SLUG_LEN",
    "NAME_TAKEN",
    "NotAsRequested",
    "OwnerKind",
    "Repo",
    "archive",
    "archived_read_only",
    "check_slug",
    "create",
    "exists",
    "get",
    "name_already_exists",
    "rename",
    "set_private",
    "valid_slug",
]

#: Not defaults to be overridden casually: see the module docstring. They are the
#: only reason an unwanted repository is survivable at all.
DEFAULT_PRIVATE = True
DEFAULT_AUTO_INIT = False

#: Execution order for the compensation. Archive is last because it makes the
#: other two impossible; rename is first because it is the one that stops the
#: name colliding with the next attempt.
COMPENSATION_ORDER: tuple[str, ...] = ("rename", "set_private", "archive")

#: GitHub's own limit on a repository name.
MAX_SLUG_LEN = 100

#: The substring GitHub puts in the 422 when the name is taken. It is the only
#: reliable collision signal: a private repository the token cannot see answers
#: 404 to a read, so "it does not exist" and "you cannot see it" are the same
#: answer and only the create itself can tell them apart.
NAME_TAKEN = "name already exists on this account"

#: The 403 that proves the compensation order matters.
ARCHIVED_READ_ONLY = "archived so is read-only"

OwnerKind = Literal["auto", "user", "org"]

#: What GitHub accepts as a repository name. Turkish text is slugged upstream
#: (ö->o, ş->s, ı->i); by the time a name reaches this module it is expected to
#: be a valid slug already, and this is the guard that says so rather than
#: letting a percent-encoded surprise into a URL path.
_SLUG = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: An owner may be a user or an organisation login. Same character class, no dots.
_OWNER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]*\Z")


class NotAsRequested(RuntimeError):
    """The call succeeded and produced something other than what was insisted on.

    Its own type, and it carries the :class:`Repo`, because both halves matter: a
    repository created PUBLIC when private was requested (an organisation policy,
    an enterprise default) breaks the one safety property this stage rests on, so
    no caller may treat it as a success — and it exists, so no caller may forget
    about it either. Raising without the object would lose it.
    """

    def __init__(self, repo: Repo, reason: str) -> None:
        super().__init__(f"{repo.full_name}: {reason}")
        self.repo = repo
        self.reason = reason


class Repo:
    """One repository, reduced to what an effect row and a spoken line need.

    ``empty`` is set from the REQUEST rather than read from the response, and is
    ``None`` when nothing knows: a freshly created repository with no commits
    still reports a ``default_branch``, so the response cannot be asked whether
    there is anything in it. ``auto_init=False`` is what makes it empty, so the
    flag that caused it is the honest source.
    """

    __slots__ = (
        "archived",
        "default_branch",
        "empty",
        "full_name",
        "html_url",
        "name",
        "node_id",
        "owner",
        "private",
    )

    def __init__(
        self,
        *,
        full_name: str,
        name: str,
        owner: str,
        node_id: str,
        html_url: str,
        private: bool,
        archived: bool = False,
        default_branch: str | None = None,
        empty: bool | None = None,
    ) -> None:
        self.full_name = full_name
        self.name = name
        self.owner = owner
        self.node_id = node_id
        self.html_url = html_url
        self.private = private
        self.archived = archived
        self.default_branch = default_branch
        self.empty = empty

    def __repr__(self) -> str:
        return (
            f"Repo({self.full_name!r}, private={self.private}, archived={self.archived}, "
            f"empty={self.empty})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Repo):
            return NotImplemented
        return self.as_provider_ref() == other.as_provider_ref()

    def __hash__(self) -> int:
        return hash((self.full_name, self.node_id, self.private, self.archived))

    def as_provider_ref(self) -> dict[str, Any]:
        """The JSON an effect row stores, so a later process can find this again.

        ``node_id`` is in here because a repository can be RENAMED — by the
        compensation itself — and after that the full name in an old row points at
        nothing. The node id is the identity that survives it.
        """
        return {
            "full_name": self.full_name,
            "node_id": self.node_id,
            "html_url": self.html_url,
            "private": self.private,
            "archived": self.archived,
            "empty": self.empty,
        }


def valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= MAX_SLUG_LEN and _SLUG.match(slug) is not None


def check_slug(slug: str) -> str:
    """Refuse a name that would not survive a URL path, rather than encoding it.

    Returned rather than mutated: a name the user cannot have is a question to ask
    them (the spoken rename loop), never a string quietly rewritten behind their
    back and then read back to them as though they had chosen it.
    """
    if not valid_slug(slug):
        raise ValueError(
            f"{slug!r} is not a usable repository name: it must match {_SLUG.pattern} and be at "
            f"most {MAX_SLUG_LEN} characters. Slug it before it gets here, and if that changes "
            "what the user said, say the new name back to them."
        )
    return slug


def _check_owner(owner: str) -> str:
    if not owner or _OWNER.match(owner) is None:
        raise ValueError(f"{owner!r} is not a usable GitHub owner login")
    return owner


def get(
    transport: Transport,
    owner: str,
    slug: str,
    *,
    timeout: float | None = None,
) -> Repo | None:
    """The repository, or None if the token cannot see one by that name.

    None is genuinely ambiguous and the docstring is where that is admitted:
    GitHub answers 404 both for a repository that does not exist and for a private
    one this token may not look at. Do not use this as the collision check for a
    create — use the create's own 422 (:func:`name_already_exists`).
    """
    path = _repo_path(owner, slug)
    try:
        resp = transport.request("GET", path, timeout=timeout)
    except NotFound:
        return None
    return _repo_from(resp, path)


def exists(
    transport: Transport,
    owner: str,
    slug: str,
    *,
    timeout: float | None = None,
) -> bool:
    """Whether a repository of that name is VISIBLE to this token.

    The cheap half of the collision check, and the reason the spoken rename loop
    usually never needs to hear a 422: it is one request, it changes nothing, and
    a False here is only provisional (see :func:`get`).
    """
    return get(transport, owner, slug, timeout=timeout) is not None


def create(
    transport: Transport,
    owner: str,
    slug: str,
    *,
    private: bool = DEFAULT_PRIVATE,
    auto_init: bool = DEFAULT_AUTO_INIT,
    description: str | None = None,
    owner_kind: OwnerKind = "auto",
    timeout: float | None = None,
) -> Repo:
    """Create the repository. IRREVERSIBLE with the token this system uses.

    ``private`` and ``auto_init`` are arguments so the call site states the
    property, and they are checked rather than trusted: any other combination
    raises before a request is made. See the module docstring for why.

    NEVER RETRIED, here or below. ``POST`` to the repos endpoint has no
    idempotency key, so a reset arriving after the server accepted the body leaves
    a repository behind that this process knows nothing about — which is the
    ``at_most_once`` case the ``outbox`` column was written for. The transport
    retries GET and HEAD only, and a caller that wants another attempt must make
    that decision durably, having first asked :func:`exists` what happened.

    ``owner_kind='auto'`` spends one extra ``GET /user`` to decide between the
    personal and the organisation endpoint. Pass it explicitly to save the call.
    """
    _check_owner(owner)
    check_slug(slug)
    if not private or auto_init:
        raise ValueError(
            "a repository Jarvis creates must be private and empty (private=True, "
            "auto_init=False): the token has no delete_repo scope, so 'harmless if wrong' is "
            "the only safety property available and jarvis.effects.github_repo_create_effect "
            "refuses to record anything else"
        )

    path = _create_path(transport, owner, owner_kind, timeout)
    body: dict[str, Any] = {"name": slug, "private": True, "auto_init": False}
    if description is not None:
        body["description"] = description
    resp = transport.request("POST", path, body=body, timeout=timeout, retry_safe=False)
    repo = _repo_from(resp, path, empty=True)
    if not repo.private:
        # The safety property did not hold. The repository exists, so the caller
        # is told both facts at once and can compensate; it must not be recorded
        # as the harmless thing it is not.
        raise NotAsRequested(repo, "created PUBLIC although private was requested")
    return repo


def rename(
    transport: Transport,
    owner: str,
    slug: str,
    new_slug: str,
    *,
    timeout: float | None = None,
) -> Repo:
    """Rename it. First in :data:`COMPENSATION_ORDER`, and the one that frees the name."""
    check_slug(new_slug)
    return _patch(transport, owner, slug, {"name": new_slug}, timeout)


def set_private(
    transport: Transport,
    owner: str,
    slug: str,
    *,
    private: bool = True,
    timeout: float | None = None,
) -> Repo:
    """Make it private (or public, if somebody explicitly asks for that)."""
    return _patch(transport, owner, slug, {"private": bool(private)}, timeout)


def archive(
    transport: Transport,
    owner: str,
    slug: str,
    *,
    archived: bool = True,
    timeout: float | None = None,
) -> Repo:
    """Archive it. LAST in :data:`COMPENSATION_ORDER`: this is what freezes the rest."""
    return _patch(transport, owner, slug, {"archived": bool(archived)}, timeout)


def name_already_exists(exc: GithubError) -> bool:
    """Whether a 422 means "that name is taken" — the spoken rename loop's cue.

    GitHub puts the reason in ``errors[].message`` rather than in ``message``, so
    a check against the top-level text alone silently never fires.
    """
    if not isinstance(exc, Unprocessable):
        return False
    haystack = [exc.message, *(str(e) for e in exc.errors)]
    return any(NAME_TAKEN in text.lower() for text in haystack)


def archived_read_only(exc: GithubError) -> bool:
    """Whether a 403 means "this repo is archived", not "you lack permission".

    Which is to say: the compensation was run out of order. Worth telling apart,
    because the answer is to unarchive and retry rather than to tell the user
    their token is wrong.
    """
    return exc.status == 403 and ARCHIVED_READ_ONLY in exc.message.lower()


# ───────────────────────────── plumbing ─────────────────────────────


def _create_path(
    transport: Transport, owner: str, owner_kind: OwnerKind, timeout: float | None
) -> str:
    """Which endpoint creates a repo for this owner.

    There are two, they are not interchangeable, and picking the wrong one is a
    404 on a path that looks perfectly reasonable: a repository in your own
    account is created by ``POST /user/repos`` with no owner in the path at all,
    while an organisation's is ``POST /orgs/{org}/repos``.
    """
    if owner_kind == "user":
        return "/user/repos"
    if owner_kind == "org":
        return f"/orgs/{owner}/repos"
    resp = transport.request("GET", "/user", timeout=timeout)
    login = resp.body.get("login") if isinstance(resp.body, dict) else None
    if isinstance(login, str) and login.lower() == owner.lower():
        return "/user/repos"
    return f"/orgs/{owner}/repos"


def _patch(
    transport: Transport,
    owner: str,
    slug: str,
    body: dict[str, Any],
    timeout: float | None,
) -> Repo:
    path = _repo_path(owner, slug)
    resp = transport.request("PATCH", path, body=body, timeout=timeout, retry_safe=False)
    return _repo_from(resp, path)


def _repo_path(owner: str, slug: str) -> str:
    return f"/repos/{_check_owner(owner)}/{check_slug(slug)}"


def _repo_from(resp: Response, path: str, *, empty: bool | None = None) -> Repo:
    body = resp.body
    if not isinstance(body, dict):
        raise GithubError(resp.status, "response was not a repository object", path=path)
    for field_name in ("full_name", "node_id", "html_url"):
        if not body.get(field_name):
            # Every one of these ends up in an effect row, and a row missing the
            # node id cannot be found again after the compensation renames it.
            raise GithubError(resp.status, f"repository response had no {field_name}", path=path)
    owner = body.get("owner") or {}
    return Repo(
        full_name=str(body["full_name"]),
        name=str(body.get("name") or str(body["full_name"]).partition("/")[2]),
        owner=str(owner.get("login") or str(body["full_name"]).partition("/")[0]),
        node_id=str(body["node_id"]),
        html_url=str(body["html_url"]),
        private=bool(body.get("private", False)),
        archived=bool(body.get("archived", False)),
        default_branch=body.get("default_branch"),
        empty=empty,
    )
