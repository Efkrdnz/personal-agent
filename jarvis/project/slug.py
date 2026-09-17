"""Spoken Turkish -> a GitHub repository name, or a refusal. Pure, both ways.

The user says "comment watcher" and gets ``comment-watcher``. They say
"İstanbul takip" and must get ``istanbul-takip`` — and this is where the one
trap in the whole stage lives.

THE TRAP. ``"İ".lower()`` is NOT ``"i"``. U+0130 LATIN CAPITAL LETTER I WITH DOT
ABOVE lowercases to ``"i" + U+0307 COMBINING DOT ABOVE`` — two characters —
and ``casefold()`` does the same. So the obvious pipeline
``name.lower().strip().replace(" ", "-")`` produces a slug with an INVISIBLE
combining character in it. GitHub either refuses it or, worse, accepts it: the
user then owns a repository whose name they can never type again, and every
later comparison against the name they *say* fails. Hence the order here:
transliterate the Turkish letters EXPLICITLY first, then decompose and drop
every combining mark, and only then casefold — with a second sweep for U+0307
because a lone combining mark left in a name is unspeakable and unsearchable.

WHY THIS IS NOT :func:`jarvis.spec.repo_slug`. That function names a local
DIRECTORY during spec tidying: it must never fail (a spec that cannot be read
back because the folder name was rejected is a worse outcome), so it truncates
to 40 characters and falls back to ``new-project``. A GitHub repository name is
the opposite kind of object — it is created once, irreversibly, on someone
else's server, and the user will type it for years — so every pathological input
RAISES here instead of being quietly turned into something surprising.

WHY 87 AND NOT 100. GitHub's limit is 100 characters, but the only compensation
this token has for a wrong repository is renaming it to
``zz-abandoned-<name>``. A 100-character name cannot be renamed that way, so the
confirmation's promise ("the most I can do is rename it to zz-abandoned-…")
would be a lie for exactly the names nobody checks. Names we CREATE are
therefore capped at ``100 - len(ABANDONED_PREFIX)``; names that merely arrive
from GitHub are checked against the full 100. The prefix is imported from
:mod:`jarvis.github.scopes`, next to the sentence that promises it, so the cap
and the promise cannot drift apart.
"""

from __future__ import annotations

import re
import unicodedata

from jarvis.github.scopes import ABANDONED_PREFIX

__all__ = [
    "ABANDONED_PREFIX",
    "MAX_NEW_REPO_NAME",
    "MAX_REPO_NAME",
    "RESERVED_NAMES",
    "EmptyName",
    "NameTooLong",
    "ReservedName",
    "SlugError",
    "abandoned_name",
    "check_repo_name",
    "is_repo_name",
    "slugify",
]

#: GitHub refuses a repository name longer than this.
MAX_REPO_NAME = 100

#: The cap for a name Jarvis CREATES, so the compensation it promises fits.
MAX_NEW_REPO_NAME = MAX_REPO_NAME - len(ABANDONED_PREFIX)

#: Names that must never be produced by transliteration. ``.`` and ``..`` are
#: path traversal wearing a project's clothes; ``.git`` would slug to ``git``,
#: which is a legal repository name and a genuinely surprising one to be handed
#: after saying "dot git".
RESERVED_NAMES = frozenset({".", "..", ".git"})

#: The dotted/dotless I family and the five other Turkish letters, folded BEFORE
#: any case operation. NFKD alone is not enough: "ş" decomposes to "s" + cedilla
#: but "ı" has no decomposition at all, so a Turkish name would keep a raw
#: non-ASCII character. "I" and "İ" both land on "i" — Turkish would lower "I"
#: to "ı", which this table then maps to "i" as well, so both readings agree.
_TURKISH = str.maketrans(
    {
        "İ": "i",
        "I": "i",
        "ı": "i",
        "Ş": "s",
        "ş": "s",
        "Ğ": "g",
        "ğ": "g",
        "Ç": "c",
        "ç": "c",
        "Ö": "o",
        "ö": "o",
        "Ü": "u",
        "ü": "u",
    }
)

#: What ``casefold()`` leaves behind on U+0130 when nothing folded it first.
_COMBINING_DOT_ABOVE = "\u0307"

#: The only shape a repository name may have here: ASCII lowercase, digits, and
#: single interior hyphens. GitHub itself is more permissive (dots, underscores),
#: but a name that has to be SAID and TYPED by a human on the phone does not
#: want a dot in it, and "no doubled hyphens" is what makes the slug predictable
#: from the words.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_NOT_NAME_CHAR = re.compile(r"[^a-z0-9]+")


class SlugError(ValueError):
    """A spoken name that cannot become a repository name.

    Carries ``spoken`` — the sentence Jarvis says — because every one of these
    is heard by a human, and the alternative is each caller inventing its own
    wording for "I did not understand that name".
    """

    def __init__(self, message: str, *, spoken: str) -> None:
        super().__init__(message)
        self.spoken = spoken


class EmptyName(SlugError):
    """Nothing survived transliteration: silence, punctuation, or emoji alone."""


class ReservedName(SlugError):
    """A name that means something to git or to a filesystem rather than to a person."""


class NameTooLong(SlugError):
    """Longer than the compensation can rename, or than GitHub accepts."""


def _fold(text: str) -> str:
    """Compare-only folding, for the reserved check. Never produces a slug."""
    return " ".join(text.translate(_TURKISH).casefold().replace(_COMBINING_DOT_ABOVE, "").split())


def slugify(spoken: str, *, max_len: int = MAX_NEW_REPO_NAME) -> str:
    """The exact repository name for what the user said, or raise.

    Raising rather than repairing is the whole point: the slug is read back to
    the user before anything is created, and a name nobody can predict is a name
    the read-back cannot make safe.
    """
    raw = " ".join(spoken.split()) if isinstance(spoken, str) else ""
    if not raw:
        raise EmptyName(
            "an empty spoken name cannot become a repository name",
            spoken="I didn't catch a name for it. What should I call it?",
        )
    if _fold(raw) in RESERVED_NAMES:
        raise ReservedName(
            f"{raw!r} is reserved",
            # The user's own words are NOT put at the front of the sentence: a
            # name like ".git" would start the spoken line with a full stop, and
            # a read-back that begins mid-punctuation is unintelligible aloud.
            spoken=(
                f"The name {raw} means something to git rather than to a person. Pick another one."
            ),
        )

    out = raw.translate(_TURKISH)
    out = unicodedata.normalize("NFKD", out)
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = out.casefold()
    # Belt and braces: a casefold on a character nothing above folded can still
    # introduce U+0307, and one invisible mark is the whole failure mode.
    out = out.replace(_COMBINING_DOT_ABOVE, "")
    slug = _NOT_NAME_CHAR.sub("-", out).strip("-")

    if not slug:
        raise EmptyName(
            f"nothing in {raw!r} survives as a repository name",
            spoken=(
                f"I couldn't make a repository name out of {raw}. "
                "Say a name using letters or numbers."
            ),
        )
    if len(slug) > max_len:
        raise NameTooLong(
            f"{slug!r} is {len(slug)} characters; the limit here is {max_len}",
            spoken=(
                f"That name comes out {len(slug)} characters long and I can only use "
                f"{max_len}. Give me a shorter one."
            ),
        )
    # A defect here would reach GitHub, so the invariant is asserted on the way
    # out rather than trusted from the substitutions above.
    check_repo_name(slug, max_len=max_len)
    return slug


def check_repo_name(name: str, *, max_len: int = MAX_REPO_NAME) -> str:
    """Return ``name`` if it is a usable repository name, else raise.

    Used on the way out of :func:`slugify` and on the way in from anywhere else
    (a name typed into Telegram, a name read off an existing clone), because the
    guarantee has to hold for names this module did not produce.
    """
    if not isinstance(name, str) or not name:
        raise EmptyName(
            "a repository name cannot be empty",
            spoken="That isn't a name I can use. What should I call it?",
        )
    if name in RESERVED_NAMES:
        raise ReservedName(
            f"{name!r} is reserved",
            spoken=(
                f"The name {name} means something to git rather than to a person. Pick another one."
            ),
        )
    if len(name) > max_len:
        raise NameTooLong(
            f"{name!r} is {len(name)} characters; the limit here is {max_len}",
            spoken=f"That name is too long — {max_len} characters is the most I can use.",
        )
    if not name.isascii() or not _NAME_RE.match(name):
        raise SlugError(
            f"{name!r} is not lowercase ASCII letters, digits and single hyphens",
            spoken=(
                "That name has characters I can't put in a repository name. "
                "Letters, numbers and hyphens only."
            ),
        )
    return name


def is_repo_name(name: str, *, max_len: int = MAX_REPO_NAME) -> bool:
    """:func:`check_repo_name` as a predicate. Never raises."""
    try:
        check_repo_name(name, max_len=max_len)
    except SlugError:
        return False
    return True


def abandoned_name(name: str) -> str:
    """The name the rename compensation moves a repository to.

    :func:`jarvis.github.scopes.abandoned_name` builds the same string and is what
    the spoken line uses; this one additionally checks that the RESULT fits in
    GitHub's 100 characters, which the cap in :data:`MAX_NEW_REPO_NAME` guarantees
    for names this system created and cannot guarantee for a repository adopted
    from somewhere else.
    """
    return check_repo_name(f"{ABANDONED_PREFIX}{check_repo_name(name)}")
