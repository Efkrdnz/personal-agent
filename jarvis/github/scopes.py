"""The capability matrix, read without creating anything — and the line it speaks.

Spike S8 proposed answering "can this token delete a repo?" by creating a
throwaway repo and trying. That works, and it leaves a repo behind on the user's
real account when the answer is no, which is the exact harm this stage is about.

Most of the answer is free. Every authenticated REST response carries
``X-OAuth-Scopes`` — the scopes the presented credential actually holds — so ONE
``GET /user`` settles delete, archive, rename and make-private for a classic
token, creates nothing, and leaves nothing behind. :func:`capabilities` is that
request; :func:`capabilities_from_response` is the pure function behind it, so
any response the system already made can refresh the matrix for free.

WHY EVERY ANSWER IS A TRI-STATE. A fine-grained personal access token does not
report its permissions in a response header at all. Neither does a GitHub App
installation token. "I could not tell" is therefore not an edge case, it is the
answer for a whole class of credentials, and collapsing it is how the spoken line
starts lying in one direction or the other: read as "no" it refuses work it could
do, read as "yes" it promises an undo that does not exist. So every operation is
``yes`` / ``no`` / ``unknown`` and the sentence says which.

THE SENTENCE IS GENERATED, NEVER WRITTEN DOWN. :func:`spoken_capability_line`
composes module-owned clauses from the matrix and then checks the finished string
against :data:`jarvis.effects.PROMISE_WORDS` — the spine's own promise
vocabulary, the same list that guards the post-hoc undo line — clause by clause,
so a promise inside a negated clause ("I can't delete it") passes and the same
words in an affirmative clause do not. A future edit that softens the wording
raises :class:`jarvis.effects.OverPromise` at runtime and in the suite, which is
the whole point of this module existing rather than a constant string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from jarvis.effects import EXACT_WORDS, PROMISE_WORDS, OverPromise, Reversibility
from jarvis.github.transport import Response, TokenKind, Transport, split_scopes

__all__ = [
    "ABANDONED_PREFIX",
    "COMPENSATIONS",
    "DELETE_SCOPE",
    "FULL_REPO_SCOPE",
    "MAX_ABANDONED_SLUG",
    "MAX_REPO_NAME",
    "NOTHING_MARKERS",
    "OPERATIONS",
    "PUBLIC_REPO_SCOPE",
    "SCOPE_HEADER",
    "UNCERTAINTY_MARKERS",
    "Capabilities",
    "Operation",
    "Tri",
    "abandoned_name",
    "capabilities",
    "capabilities_from_response",
    "implied_reversibility",
    "implied_undo_plan",
    "spoken_capability_line",
]

Tri = Literal["yes", "no", "unknown"]
YES: Tri = "yes"
NO: Tri = "no"
UNKNOWN: Tri = "unknown"

Operation = Literal["create", "delete", "archive", "rename", "set_private"]

#: Every operation the matrix answers for. ``create`` is here because a token
#: that cannot create is worth knowing about BEFORE the confirmation, not after.
OPERATIONS: tuple[Operation, ...] = ("create", "delete", "archive", "rename", "set_private")

#: The three that make up the compensation. Their availability is what decides
#: whether repo creation has an undo, a compensation, or nothing at all.
COMPENSATIONS: tuple[Operation, ...] = ("archive", "rename", "set_private")

SCOPE_HEADER = "x-oauth-scopes"
FULL_REPO_SCOPE = "repo"
PUBLIC_REPO_SCOPE = "public_repo"
DELETE_SCOPE = "delete_repo"

#: The prefix a compensated repository is renamed to. It lives here, next to the
#: sentence that PROMISES it, because the promise and the name must not drift
#: apart — ``tests/test_github_scopes.py`` asserts the spoken line names exactly
#: what :func:`jarvis.github.repos.rename` would be called with.
ABANDONED_PREFIX = "zz-abandoned-"

#: GitHub's repository-name limit, which the prefix has to fit INSIDE. Duplicated
#: as data rather than imported from :mod:`jarvis.github.repos`, for the same
#: reason :data:`_EXECUTION_ORDER` is — the capability reader stays independent of
#: the client that acts on it, and ``tests/test_github_scopes.py`` asserts the two
#: agree.
MAX_REPO_NAME = 100

#: The longest slug this module will PROMISE to rename. Past it the abandoned name
#: is longer than GitHub allows, :func:`jarvis.github.repos.rename` refuses it
#: before a request is made, and the sentence spoken at the confirmation was an
#: offer that could never have been performed — for exactly the long names nobody
#: tries by hand.
MAX_ABANDONED_SLUG = MAX_REPO_NAME - len(ABANDONED_PREFIX)

#: A slug reaching the spoken line is CALLER text, and the honesty check below
#: runs on the finished sentence. A slug carrying "I can" or an apostrophe could
#: therefore shift a clause boundary or a negation. Anything outside this
#: pattern is refused rather than sanitised, because a name the user cannot have
#: is a question to ask them, not a string to quietly rewrite.
_SAFE_SLUG = re.compile(rf"\A[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_REPO_NAME - 1}}}\Z")


def abandoned_name(slug: str) -> str:
    """The name a repository is renamed to when it is abandoned.

    Refuses a slug too long to carry the prefix, rather than returning a name the
    rename would reject: this string is spoken as a promise before anything is
    created, so a name that cannot be used is a question for the user.
    """
    return f"{ABANDONED_PREFIX}{_safe_slug(slug)}"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this credential can do to a repository, and how we know.

    ``scopes`` is ``None`` when the header was absent — which is a different fact
    from an empty tuple (a classic token holding no scopes at all), and the two
    lead to different answers, so they are not collapsed.
    """

    token_kind: TokenKind
    create: Tri = UNKNOWN
    delete: Tri = UNKNOWN
    archive: Tri = UNKNOWN
    rename: Tri = UNKNOWN
    set_private: Tri = UNKNOWN
    login: str | None = None
    scopes: tuple[str, ...] | None = None
    source: str = "unread"
    notes: tuple[str, ...] = field(default_factory=tuple)

    def of(self, operation: str) -> Tri:
        """The answer for one operation. Raises on an operation nobody defined."""
        if operation not in OPERATIONS:
            raise KeyError(f"no such operation: {operation!r}; known: {list(OPERATIONS)}")
        return getattr(self, operation)  # type: ignore[no-any-return]

    @property
    def available_compensations(self) -> tuple[Operation, ...]:
        return tuple(op for op in COMPENSATIONS if self.of(op) == YES)

    @property
    def unknown_compensations(self) -> tuple[Operation, ...]:
        return tuple(op for op in COMPENSATIONS if self.of(op) == UNKNOWN)

    @property
    def any_unknown(self) -> bool:
        return any(self.of(op) == UNKNOWN for op in OPERATIONS)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, for an event payload or an effect's ``provider_ref``.

        The credential is nowhere in here: a kind, a login and a scope list are
        all public facts about the account, and the scope list is what a later
        reader needs to explain why the wording said what it said.
        """
        return {
            "token_kind": self.token_kind,
            "login": self.login,
            "scopes": None if self.scopes is None else list(self.scopes),
            "source": self.source,
            "notes": list(self.notes),
            **{op: self.of(op) for op in OPERATIONS},
        }


def capabilities(transport: Transport, *, timeout: float | None = None) -> Capabilities:
    """Read the matrix with ONE request that changes nothing.

    ``GET /user`` is the cheapest authenticated call there is: it costs one unit
    of a 5,000/hour budget, it creates nothing, and its response headers carry
    the granted scopes. The whole destructive half of spike S8 exists only for
    what a header cannot tell us — see ``tools/probe_github_token.py``.
    """
    resp = transport.request("GET", "/user", timeout=timeout)
    login = resp.body.get("login") if isinstance(resp.body, dict) else None
    return capabilities_from_response(resp, kind=transport.token_kind(), login=login)


def capabilities_from_response(
    resp: Response,
    *,
    kind: TokenKind,
    login: str | None = None,
) -> Capabilities:
    """The matrix from any authenticated response. Pure, and the real logic.

    Pure because the headers arrive on EVERY response, including the error ones:
    a 403 that turned out to be a permission problem can refresh this for free,
    and a caller that already made a request should not make another.
    """
    raw = resp.header(SCOPE_HEADER)
    if raw is None:
        return _unreadable(
            kind,
            login,
            "header-absent",
            f"no {SCOPE_HEADER} header: a fine-grained token, a GitHub App token or a "
            "credential GitHub does not describe this way",
        )
    scopes = split_scopes(raw)
    if not scopes:
        if kind in ("classic", "oauth"):
            return Capabilities(
                token_kind=kind,
                create=NO,
                delete=NO,
                archive=NO,
                rename=NO,
                set_private=NO,
                login=login,
                scopes=(),
                source="header-empty",
                notes=(f"{SCOPE_HEADER} is empty: this token holds no scopes at all",),
            )
        return _unreadable(
            kind,
            login,
            "header-empty",
            f"{SCOPE_HEADER} is empty and the token kind is {kind!r}, so the empty header "
            "cannot be read as 'no scopes' with any confidence",
        )
    return _from_scopes(kind, login, scopes)


def _unreadable(kind: TokenKind, login: str | None, source: str, note: str) -> Capabilities:
    """Every answer ``unknown``. The honest shape, and a common one."""
    return Capabilities(token_kind=kind, login=login, source=source, notes=(note,))


def _from_scopes(kind: TokenKind, login: str | None, scopes: tuple[str, ...]) -> Capabilities:
    """The classic-scope reading. Every branch here is a documented scope rule."""
    full = FULL_REPO_SCOPE in scopes
    public_only = not full and PUBLIC_REPO_SCOPE in scopes
    notes: list[str] = []

    if DELETE_SCOPE in scopes and full:
        delete: Tri = YES
    elif DELETE_SCOPE in scopes:
        # delete_repo is about admin rights, not visibility. Without `repo` the
        # token cannot even see a private repository, so whether the delete would
        # reach ours is genuinely not knowable from the header.
        delete = UNKNOWN
        notes.append(
            f"{DELETE_SCOPE} is granted but {FULL_REPO_SCOPE} is not, so whether it reaches a "
            "PRIVATE repository is not answerable from the scope list"
        )
    else:
        delete = NO
        notes.append(f"{DELETE_SCOPE} is not granted, which is the deliberate configuration")

    if full:
        create: Tri = YES
        compensations: Tri = YES
    elif public_only:
        create = NO
        compensations = UNKNOWN
        notes.append(
            f"{PUBLIC_REPO_SCOPE} covers public repositories only: it cannot create the private "
            "repo this system insists on, and what it can do to one is not knowable from here"
        )
    else:
        create = NO
        compensations = NO
        notes.append(
            f"neither {FULL_REPO_SCOPE} nor {PUBLIC_REPO_SCOPE} is granted, so this token "
            "cannot administer a repository at all"
        )

    return Capabilities(
        token_kind=kind,
        create=create,
        delete=delete,
        archive=compensations,
        rename=compensations,
        set_private=compensations,
        login=login,
        scopes=scopes,
        source=SCOPE_HEADER,
        notes=tuple(notes),
    )


# ───────────────────────────── the spoken line ─────────────────────────────

#: Clauses, module-owned. Nothing here interpolates caller text except the
#: validated slug, because :func:`_assert_honest` checks the finished sentence
#: and free text would make it fire on the user's words instead of our promise.
_CLAUSES: dict[str, str] = {
    "reversible": "I can delete it afterwards, so this one really can be undone",
    "can": "I can {actions}",
    "no_delete": "I can't delete it",
    "delete_unknown": "I don't know whether I could delete it",
    "unsure": "I'm not sure I could {actions}",
    "nothing_certain": "there is nothing I can do about it afterwards",
    "nothing_assumed": "assume nothing can be done about it afterwards",
}

#: One phrase per operation, and the ONLY place each is written. The rename
#: phrase names the same string :func:`abandoned_name` produces, so the sentence
#: cannot promise one name while the compensation performs another.
_PHRASES: dict[Operation, str] = {
    "archive": "archive it",
    "rename": "rename it to {abandoned}",
    "set_private": "make it private",
}

#: What each operation is called in the finished sentence, for the check below.
#: Verbs rather than whole phrases: "I can't archive or rename it" must read as a
#: refusal of both, and it does not contain either full phrase.
_VERBS: dict[Operation, str] = {
    "delete": "delete",
    "archive": "archive",
    "rename": "rename",
    "set_private": "private",
}

#: A sentence that cannot act has to SAY so, not merely avoid promising. Deleting
#: the honest half must fail as loudly as adding a dishonest half.
NOTHING_MARKERS: tuple[str, ...] = (
    "nothing i can do",
    "nothing can be done",
)

#: And a sentence that does not know has to say THAT, rather than picking a side.
UNCERTAINTY_MARKERS: tuple[str, ...] = (
    "don't know",
    "not sure",
)

#: Words that turn a clause into a refusal. A promise phrase inside a clause
#: carrying one of these is honest wording, not a lie — which is the only way
#: "I can't delete it" can be said at all, since "delete it" is itself in
#: :data:`jarvis.effects.PROMISE_WORDS`.
_NEGATORS: tuple[str, ...] = (
    "can't",
    "cannot",
    "can not",
    "couldn't",
    "could not",
    "won't",
    "will not",
    "unable",
    "not sure",
    "don't know",
    "do not know",
    "nothing",
    "no way",
    "never",
)

#: What the abandoned name is replaced by before the honesty check reads the
#: sentence. Deliberately free of every operation verb and every negator.
_NAME_MASK = "that other name"

#: Clause boundaries. Conjunctions and sentence ends, plus a comma that starts a
#: new subject (", I can …") — but NOT a plain list comma, because "archive it,
#: rename it to X and make it private" is one promise with three parts and
#: splitting it would strand the parts without the "I can" that governs them.
_CLAUSE_BREAK = re.compile(r"\s+but\s+|\s+and\s+|\s+so\s+|;|\s+—\s+|\.\s*|,\s+(?=i[\s'])")


def spoken_capability_line(caps: Capabilities, *, slug: str) -> str:
    """THE honest half of the confirmation, DERIVED from the measured matrix.

    Said before anything is created, because that is the only moment it is useful:

    * delete available          -> it really can be undone;
    * archive + rename + private -> "I can archive it, rename it to
      zz-abandoned-<slug> and make it private, but I can't delete it";
    * none of those             -> there is nothing I can do about it afterwards;
    * anything unknown          -> said as unknown, in neither direction.

    Raises :class:`jarvis.effects.OverPromise` rather than speaking a lie.
    """
    abandoned = abandoned_name(slug)
    line = _compose(caps, abandoned)
    _assert_honest(caps, line, abandoned=abandoned)
    return line


def _compose(caps: Capabilities, abandoned: str) -> str:
    if caps.delete == YES:
        return _sentence([("", _CLAUSES["reversible"])])

    available = [_PHRASES[op].format(abandoned=abandoned) for op in caps.available_compensations]
    unsure = [_PHRASES[op].format(abandoned=abandoned) for op in caps.unknown_compensations]

    parts: list[tuple[str, str]] = []
    if available:
        parts.append(("", _CLAUSES["can"].format(actions=_join(available, "and"))))
    parts.append(
        (
            ", but " if parts else "",
            _CLAUSES["no_delete"] if caps.delete == NO else _CLAUSES["delete_unknown"],
        )
    )
    if unsure:
        parts.append((", and ", _CLAUSES["unsure"].format(actions=_join(unsure, "or"))))
    if not available:
        # Nothing to offer. Whether that is a fact or a guess depends on whether
        # anything in the matrix came back unknown, and the two must not be said
        # in the same words.
        certain = not unsure and caps.delete == NO
        parts.append((", so ", _CLAUSES["nothing_certain" if certain else "nothing_assumed"]))
    return _sentence(parts)


def _sentence(parts: list[tuple[str, str]]) -> str:
    text = "".join(joiner + clause for joiner, clause in parts)
    return f"{text[0].upper()}{text[1:]}."


def _join(items: list[str], conjunction: str) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def _assert_honest(caps: Capabilities, line: str, *, abandoned: str = "") -> None:
    """Make the honesty requirement mechanical rather than remembered.

    Run on the finished sentence every single time one is generated, so an edit
    that lets the wording over-promise fails here — in the suite and at runtime —
    instead of being discovered by a user who believed it.

    Four things are checked, and each one is a different way to lie:

    1. no operation may be CLAIMED unless the matrix says ``yes``. A claim is the
       operation's verb in a clause with no negation in it, so "I can't archive
       or rename it" is not a claim and "I can archive it" is;
    2. when deletion is not available the sentence must SAY so out loud, and must
       not carry the spine's promise or restoration vocabulary in an affirmative
       clause;
    3. when anything is unknown the sentence must be audibly uncertain;
    4. when there is nothing to offer it must say that, so deleting the honest
       half fails as loudly as adding a dishonest half.
    """
    # The abandoned NAME is masked out first. It is derived from the user's own
    # words, so "delete-me" or "archive-bot" is a perfectly ordinary project name,
    # and the rename phrase that carries it would otherwise read as a claim to
    # delete or to archive — failing the honesty check on a sentence that is
    # entirely honest. Masking is safe because the name is DATA: it is never the
    # part of the sentence that promises anything.
    low = line.lower()
    if abandoned:
        low = low.replace(abandoned.lower(), _NAME_MASK)
    affirmative = [c for c in _clauses_of(low) if not any(n in c for n in _NEGATORS)]

    for op in ("delete", *COMPENSATIONS):
        if caps.of(op) != YES and any(_VERBS[op] in clause for clause in affirmative):
            raise OverPromise(
                f"the line claims it can {op} while the matrix says {caps.of(op)!r}: {line!r}"
            )

    if caps.delete != YES:
        for word in (*PROMISE_WORDS, *EXACT_WORDS):
            if any(word in clause for clause in affirmative):
                raise OverPromise(
                    f"a line for a token that cannot delete promises {word!r}: {line!r}"
                )
        if _VERBS["delete"] not in low:
            raise OverPromise(
                f"the line never says what happens about deletion, which is the one thing "
                f"the user needs to hear: {line!r}"
            )

    if _unknown_that_matters(caps) and not any(m in low for m in UNCERTAINTY_MARKERS):
        raise OverPromise(f"the matrix has an unknown and the line sounds certain: {line!r}")

    nothing_to_offer = caps.delete != YES and not caps.available_compensations
    if nothing_to_offer and not any(m in low for m in NOTHING_MARKERS):
        raise OverPromise(
            f"nothing can be done and the line does not say so in words a user would "
            f"hear as a refusal: {line!r}"
        )


def _unknown_that_matters(caps: Capabilities) -> bool:
    """Which unknowns the sentence has to admit to.

    Not every one. With deletion available the sentence is about deletion and the
    compensations are moot, and ``create`` is answered before the confirmation
    rather than in it — so demanding hedged wording for those would push the
    uncertainty marker into sentences where it is noise, and a marker that means
    nothing gets deleted.
    """
    if caps.delete == YES:
        return False
    return caps.delete == UNKNOWN or bool(caps.unknown_compensations)


def _clauses_of(low: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_BREAK.split(low) if part.strip()]


def _safe_slug(slug: str) -> str:
    if not _SAFE_SLUG.match(slug or ""):
        raise ValueError(
            f"a slug reaching the spoken line must match {_SAFE_SLUG.pattern} so it cannot "
            f"shift a clause boundary or a negation in it, got {slug!r}"
        )
    if len(slug) > MAX_ABANDONED_SLUG:
        raise ValueError(
            f"{slug!r} is {len(slug)} characters, so {ABANDONED_PREFIX}<name> would be longer "
            f"than the {MAX_REPO_NAME} characters GitHub allows and the rename this line offers "
            f"could not be performed. A name Jarvis creates must be at most "
            f"{MAX_ABANDONED_SLUG} characters; ask the user for a shorter one rather than "
            "promising a compensation that would fail."
        )
    return slug


# ───────────────────────── the ledger's view of it ─────────────────────────


def implied_reversibility(caps: Capabilities) -> Reversibility:
    """The class the MEASURED capabilities imply for a repo creation.

    Pessimistic on purpose: ``unknown`` yields the worst class that fits, because
    the class chooses the confirmation strength and over-confirming costs a
    sentence while under-confirming costs a repo the user did not want.

    IT IS NOT THE CLASS THE LEDGER WILL STORE.
    :data:`jarvis.effects.KIND_REVERSIBILITY` classifies ``github.repo_create``
    as ``irreversible`` and :func:`jarvis.effects.record_effect` refuses anything
    more optimistic than the table — deliberately, since that is what keeps the
    gate at ``confirm_readback``. So this function describes what Jarvis can say
    BEFORE acting; if a measured token turns out to be delete-capable, the
    table's entry is the thing to revisit, in the spine, with its own test.
    """
    if caps.delete == YES:
        # As close to "never happened" as a remote side effect gets — though a
        # webhook that already fired and a notification already sent do not come
        # back, which is why the spine's own table is more pessimistic than this.
        return "reversible"
    if caps.available_compensations:
        return "compensatable"
    return "irreversible"


def implied_undo_plan(caps: Capabilities, *, owner: str, slug: str) -> dict[str, Any] | None:
    """The declarative plan the capabilities support, or None if they support none.

    ``speaks`` is generated from the same phrases the spoken line uses, so the
    sentence the ledger will say later and the sentence said at the confirmation
    describe the same actions. ``args`` names the operations in the order they
    must be performed — see
    :data:`jarvis.github.repos.COMPENSATION_ORDER`, and the reason archiving is
    last is that it makes everything else impossible.
    """
    ops = caps.available_compensations
    if not ops:
        return None
    ordered = tuple(op for op in _EXECUTION_ORDER if op in ops)
    abandoned = abandoned_name(slug)
    phrases = [_PHRASES[op].format(abandoned=abandoned) for op in ops]
    return {
        "op": "github.repo_compensate",
        "args": {
            "owner": owner,
            "slug": slug,
            "operations": list(ordered),
            "rename_to": abandoned if "rename" in ops else None,
        },
        "speaks": _join(phrases, "and"),
    }


#: Duplicated as data rather than imported, so :mod:`jarvis.github.repos` never
#: has to import this module. ``tests/test_github_repos.py`` asserts the two
#: tuples agree, which is cheaper than a circular dependency between the
#: capability reader and the thing it describes.
_EXECUTION_ORDER: tuple[Operation, ...] = ("rename", "set_private", "archive")
