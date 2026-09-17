"""The four sections: what each one says, and what it remembers having said.

A section is composed ONCE and then stored (``briefing_sections.content``), which
is what makes "repeat" repeat the same words and a re-attach on another channel
mid-briefing continue the same briefing rather than a newer one. Composition is
therefore a pure-ish function of (rows, cursor, seen set, source answers) with
one side effect: none. Nothing here writes a cursor. See
:mod:`jarvis.briefing.navigator` for why that matters.

THE ORDER IS THE ORDER. Project status first because it is the only section that
can contain something the user has to act on before doing anything else; the
inbox second because that is where the day's obligations arrive; issues and
comments after, because they are other people's asks and can wait ten minutes.

WHAT A SECTION SAYS WHEN ITS SOURCE IS DOWN IS THE POINT. "No new issues" and "I
could not check for issues" are different statements, and only the second one
tells the user something is broken. So an unreadable source produces a section
with ``ok=False``, a sentence that says so, NO items remembered and NO cursor
advanced — tomorrow asks the same question of the same cursor and nothing is
lost.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis.briefing.sources import (
    CandidateItem,
    Fetch,
    Source,
    Triage,
    gmail,
    unconfigured,
    youtube,
)
from jarvis.briefing.sources import projects as projects_source
from jarvis.briefing.sources.github import GITHUB_CURSOR
from jarvis.briefing.sources.gmail import GMAIL_CURSOR
from jarvis.briefing.sources.projects import PROJECTS_CURSOR
from jarvis.briefing.sources.youtube import YOUTUBE_CURSOR
from jarvis.ids import now

__all__ = [
    "MAX_SPOKEN",
    "SECTION_KEYS",
    "SPECS",
    "SectionContent",
    "SectionSpec",
    "Sources",
    "compose",
    "spec_for",
]

#: How many items one section reads out. The rest are COUNTED in the same breath
#: ("and nine more I haven't read out"), never dropped in silence — and they are
#: still remembered as said, because a briefing that re-reads the same tail every
#: morning is the one people stop listening to.
MAX_SPOKEN = 5


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """One section's identity and its sentences. No logic, so it can be data."""

    key: str
    #: Spoken inside "skip to …", so it is a noun phrase and not a heading. It is
    #: NOT a sentence subject: "new issues" reads correctly in "skip to new
    #: issues" and not in anything else, which is why `not_connected` below is
    #: written out rather than built from it.
    title: str
    cursor: str
    nothing: str
    one: str
    many: str
    #: Said when no source for this section was injected at all. A whole sentence,
    #: because every line of a briefing is spoken verbatim. No default: a section
    #: added later must decide what it says when nobody wired it up.
    not_connected: str


SPECS: tuple[SectionSpec, ...] = (
    SectionSpec(
        key="projects",
        title="your projects",
        cursor=PROJECTS_CURSOR,
        # Deliberately NOT jarvis.reconcile's own "nothing has finished or failed"
        # fallback: that sentence means "there was nothing", this one means
        # "there was nothing you have not already heard". Different facts.
        nothing="Nothing new about your projects since I last told you.",
        one="One thing happened with your projects.",
        many="{n} things happened with your projects.",
        # Section one reads the same database the briefing lives in, so there is
        # nothing to connect and no path that can reach this sentence.
        not_connected="I could not read your project status.",
    ),
    SectionSpec(
        key="inbox",
        title="your inbox",
        cursor=GMAIL_CURSOR,
        nothing="Nothing new in your inbox worth reading out.",
        one="One new message that looks like it matters.",
        many="{n} new messages that look like they matter.",
        not_connected=gmail.NOT_CONNECTED,
    ),
    SectionSpec(
        key="issues",
        title="new issues",
        cursor=GITHUB_CURSOR,
        nothing="No new issues on your repositories.",
        one="One new issue on your repositories.",
        many="{n} new issues on your repositories.",
        not_connected="GitHub is not connected yet, so I could not check your issues.",
    ),
    SectionSpec(
        key="comments",
        title="your YouTube comments",
        cursor=YOUTUBE_CURSOR,
        nothing="No new comments on your channel.",
        one="One new comment on your channel.",
        many="{n} new comments on your channel.",
        not_connected=youtube.NOT_CONNECTED,
    ),
)

SECTION_KEYS: tuple[str, ...] = tuple(s.key for s in SPECS)

_BY_KEY: dict[str, SectionSpec] = {s.key: s for s in SPECS}


def spec_for(key: str) -> SectionSpec:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"no such briefing section {key!r}; there are {list(_BY_KEY)}") from None


@dataclass(frozen=True, slots=True)
class Sources:
    """The three remote sources, injected. ``None`` means "not connected".

    ``None`` is the state this machine is actually in for Gmail and YouTube —
    there are no Google credentials — and it produces a section that says so
    rather than one that says nothing. Section one is absent from this bundle on
    purpose: it reads the same database the briefing lives in and so takes the
    connection, like everything else in the tree.
    """

    inbox: Source | None = None
    issues: Source | None = None
    comments: Source | None = None
    triage: Triage | None = None

    def of(self, key: str) -> Source | None:
        return {"inbox": self.inbox, "issues": self.issues, "comments": self.comments}.get(key)


@dataclass(frozen=True, slots=True)
class SectionContent:
    """One composed section: the words, and what saying them would settle.

    ``item_ids`` and ``next_cursor`` are a PROMISE, not a fact: they are what the
    briefing will remember and where the cursor will move *if and when* this
    section actually reaches a human. Keeping them on the composed object rather
    than applying them at compose time is the whole of the "a briefing composed
    into an empty room must be said again" rule.
    """

    key: str
    title: str
    lines: tuple[str, ...]
    ok: bool = True
    error: str | None = None
    item_ids: tuple[str, ...] = ()
    cursor_name: str | None = None
    next_cursor: str | None = None
    composed_at: str = ""
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def spoken(self) -> str:
        """The section as one utterance. Verbatim: this text is never paraphrased."""
        return " ".join(self.lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "lines": list(self.lines),
            "ok": self.ok,
            "error": self.error,
            "item_ids": list(self.item_ids),
            "cursor_name": self.cursor_name,
            "next_cursor": self.next_cursor,
            "composed_at": self.composed_at,
            "notes": dict(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SectionContent:
        return cls(
            key=str(raw["key"]),
            title=str(raw.get("title", "")),
            lines=tuple(str(line) for line in raw.get("lines") or ()),
            ok=bool(raw.get("ok", True)),
            error=raw.get("error"),
            item_ids=tuple(str(i) for i in raw.get("item_ids") or ()),
            cursor_name=raw.get("cursor_name"),
            next_cursor=raw.get("next_cursor"),
            composed_at=str(raw.get("composed_at", "")),
            notes={str(k): str(v) for k, v in (raw.get("notes") or {}).items()},
        )


def _count_line(spec: SectionSpec, n: int) -> str:
    return spec.one if n == 1 else spec.many.format(n=n)


def _fetch(
    con: sqlite3.Connection,
    key: str,
    sources: Sources,
    cursor: str | None,
    ts: str,
) -> Fetch:
    """Ask one source, and survive anything it does.

    The three shipped sources already convert their own failures into
    ``ok=False``. This catch is the second belt: a source written later — or a
    Google client raising from a stack nothing here has seen — must not be able
    to take the whole briefing down, because a briefing that fails to arrive is
    strictly worse than one section of it admitting it is blind.
    """
    if key == "projects":
        return projects_source.fetch(con, cursor, now_ts=ts)
    source = sources.of(key)
    if source is None:
        return unconfigured(spec_for(key).not_connected)
    try:
        return source.fetch(cursor)
    except Exception as exc:  # noqa: BLE001 - see the docstring; this is the point
        return Fetch(ok=False, error=f"I could not check {spec_for(key).title}: {exc}")


def _triaged(
    items: Sequence[CandidateItem], triage: Triage | None
) -> tuple[tuple[CandidateItem, ...], str | None]:
    """Rank, or say out loud that ranking was not possible. Never silently unranked."""
    if triage is None or not items:
        return tuple(items), None
    try:
        scored = triage.score(items)
    except Exception as exc:  # noqa: BLE001 - triage is a model call; it will fail
        return tuple(items), f"I could not work out which of these matter ({exc})."
    ranked = tuple(s.item for s in scored)
    known = {i.id for i in items}
    # A triage that drops or invents items would silently lose mail. Refuse its
    # ordering rather than its content: the section still says everything.
    if len(ranked) != len(items) or {i.id for i in ranked} != known:
        return tuple(items), "I could not work out which of these matter."
    return ranked, None


def compose(
    con: sqlite3.Connection,
    key: str,
    *,
    sources: Sources | None = None,
    cursor: str | None = None,
    seen: Collection[str] = (),
    now_ts: str | None = None,
) -> SectionContent:
    """Build one section. Reads; writes nothing, not even a cursor.

    ``seen`` is the exactness set for this section's source. The cursors are
    inclusive by design (:func:`jarvis.reconcile.set_briefing_cursor` explains
    why), so the boundary item comes back every run and this is what removes it.
    """
    spec = spec_for(key)
    ts = now_ts or now()
    bundle = sources or Sources()
    got = _fetch(con, key, bundle, cursor, ts)

    if not got.ok:
        return SectionContent(
            key=spec.key,
            title=spec.title,
            lines=(got.error or f"I could not check {spec.title}.",),
            ok=False,
            error=got.error,
            cursor_name=spec.cursor,
            # No items, no cursor: an unread source must cost nothing, so that
            # tomorrow asks the same question of the same cursor.
            next_cursor=None,
            composed_at=ts,
            notes=dict(got.notes),
        )

    fresh = tuple(i for i in got.items if i.id not in seen)
    ordered, triage_note = _triaged(fresh, bundle.triage) if key == "inbox" else (fresh, None)

    lines: list[str] = []
    if ordered:
        lines.append(_count_line(spec, len(ordered)))
        lines.extend(i.line for i in ordered[:MAX_SPOKEN])
        held = len(ordered) - MAX_SPOKEN
        if held > 0:
            # Counted out loud. The alternative — saying five and forgetting the
            # rest — is the silent loss this whole module is built to avoid.
            lines.append(
                "There is one more I haven't read out."
                if held == 1
                else f"There are {held} more I haven't read out."
            )
    if triage_note:
        lines.append(triage_note)
    lines.extend(got.standing)
    if not lines:
        lines.append(spec.nothing)

    return SectionContent(
        key=spec.key,
        title=spec.title,
        lines=tuple(lines),
        ok=True,
        item_ids=tuple(i.id for i in ordered),
        cursor_name=spec.cursor,
        next_cursor=got.next_cursor,
        composed_at=ts,
        notes=dict(got.notes),
    )
