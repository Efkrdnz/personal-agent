"""What a briefing section reads FROM, as one protocol and one failure shape.

Three of the four sections are "new things since last time" against a remote
service, and every one of those services fails differently: Gmail 500s, a GitHub
token expires overnight, YouTube's quota runs out. So the protocol has ONE
method and its return value carries the failure rather than raising it.

WHY ``ok`` IS A FIELD AND NOT AN EXCEPTION. "No new issues" and "I could not
check for issues" are completely different statements, and the second one is the
one that matters — it is the only one that tells the user something is broken.
An exception would have to be caught somewhere and turned back into a sentence
anyway, and the tempting catch ("well, show the rest of the briefing") is
exactly the one that produces a silently empty section. Here the empty result
and the unreadable result are different values of the same type and a composer
cannot conflate them without saying so in code.

Sources are called with the stored cursor and hand back the cursor they want
stored NEXT — but they do not store it. Storing is the composer's job and
happens only after the section was really delivered, because a briefing composed
into an empty room must be said again tomorrow.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "CandidateItem",
    "Fetch",
    "Scored",
    "Source",
    "Triage",
    "down",
    "unconfigured",
]


@dataclass(frozen=True, slots=True)
class CandidateItem:
    """One thing that might be worth saying out loud.

    ``id`` is the identity the ``briefing_seen`` set is keyed on, so it must be
    STABLE for the same underlying thing across runs — a Gmail message id, a
    GitHub issue node id, a YouTube comment id. A hash of the rendered sentence
    would look fine and would repeat the item the moment anybody edited it.

    ``at`` is the item's own timestamp and is what an advancing cursor is built
    from. ``None`` is allowed: a source whose items carry no usable time keeps
    its cursor where it was and relies on the seen set alone.
    """

    id: str
    line: str
    at: str | None = None
    detail: str = ""
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Fetch:
    """What one source had to say, including that it had nothing to say WHY.

    ``ok=False`` means the section must report that it could not be read. The
    composer then advances no cursor and remembers no items, so nothing is lost
    by a bad morning: tomorrow asks the same question of the same cursor.
    """

    items: tuple[CandidateItem, ...] = ()
    #: Sentences that are TRUE NOW rather than new, said every run and never
    #: deduped. Only section one has any: "the todo app has been waiting two
    #: hours" must be repeated until it stops being true, while "the build
    #: failed" happened once. The three remote sources leave this empty, and the
    #: field lives here rather than in a section-one-shaped return type because a
    #: composer that had to handle two shapes would grow a branch per section.
    standing: tuple[str, ...] = ()
    next_cursor: str | None = None
    ok: bool = True
    error: str | None = None
    #: Free-form and never spoken: quota headroom, a resync notice, a page count.
    notes: dict[str, str] = field(default_factory=dict)


def down(error: str) -> Fetch:
    """The source answered badly, or not at all. One place, so the shape is one shape."""
    return Fetch(ok=False, error=error)


def unconfigured(sentence: str) -> Fetch:
    """No credential for this source at all — a different fact from "it broke".

    This is the honest state of Gmail and YouTube in this stage: there are no
    Google credentials on this machine or in CI. A section that reported "nothing
    new" here would be claiming to have looked.

    TAKES THE WHOLE SENTENCE, and that is the fix for a real bug rather than a
    style preference. This used to be ``f"{what} is not connected yet"`` over a
    section's spoken TITLE, and the titles are written to be said inside "skip to
    …" — so the issues section, whose title is "new issues", said *"new issues is
    not connected yet"* out loud, in a section marked ``verbatim``. English is
    not a format string; every sentence Jarvis says is written down as itself.
    """
    return Fetch(ok=False, error=sentence)


@dataclass(frozen=True, slots=True)
class Scored:
    """One candidate, with how much it matters and WHY it got that score.

    ``reason`` exists because the inbox triage will eventually be a model, and a
    ranking nobody can interrogate is one nobody can correct.
    """

    item: CandidateItem
    score: float
    reason: str = ""


@runtime_checkable
class Source(Protocol):
    """One remote thing, read once per briefing.

    ``name`` is the row in ``cursors`` this source owns; it is also the key its
    items are remembered under in ``briefing_seen``, so two sources can never
    suppress each other's items by id collision.
    """

    name: str

    def fetch(self, cursor: str | None) -> Fetch:
        """Everything at or after ``cursor``. Inclusive; the seen set dedupes."""


@runtime_checkable
class Triage(Protocol):
    """Decide which of these actually matter. Not a filter — a RANKING.

    Separate from :class:`Source` because the two fail independently and at
    different costs: fetching is a network call with a quota, scoring is a model
    call with a bill. A source that works while triage is down should still be
    able to say "here are twelve new messages, I could not tell you which
    matter", which is only expressible if they are two objects.
    """

    def score(self, items: Sequence[CandidateItem]) -> tuple[Scored, ...]:
        """Score every item. Returns them in the order they should be said."""
