"""Section two: what actually matters in the inbox. A SEAM, because there is no credential.

THERE ARE NO GOOGLE CREDENTIALS on this machine or in CI, now or while this
stage is being built, so nothing here may need one to import or to test. What
ships is the shape: a :class:`~jarvis.briefing.sources.Source` that yields
candidate messages, a :class:`~jarvis.briefing.sources.Triage` that ranks them,
and a deterministic fake for each. Wiring a real Google client in later means
writing one class that satisfies :class:`GmailApi` — and the paragraph below is
the answer to the question that class will otherwise get wrong.

THE REAL DESIGN, so whoever wires it does not have to re-derive it:

*Read the delta with* ``users.history.list(userId='me', startHistoryId=<stored>,
historyTypes=['messageAdded'], labelId='INBOX')`` *and store the ``historyId``
it returns.* Gmail's history is the only incremental read it offers that is
exact. Every page carries a ``historyId``; the one to store is the one from the
LAST page, and only after the section was actually delivered.

*A 404 from that call is normal and means the stored id has aged out* — Gmail
keeps roughly a week of history and will not say how much. The handler is a FULL
RESYNC: ``users.messages.list(labelId='INBOX', maxResults=N)`` newest-first,
then one batched ``users.messages.get(format='metadata',
metadataHeaders=['From','Subject','List-Unsubscribe','Date'])`` per page. It is
not an error path to log; it is a branch to take. :class:`HistoryGone` is its
own exception here so it cannot be caught as a generic failure and turned into
an empty inbox section.

*Do NOT use* ``q=after:<date>``. Two measured reasons, both fatal for a daily
briefing: the ``after:`` operator has DAY granularity, so a 10:00 briefing
either re-reads the whole of today or silently drops everything that arrived
this morning; and the search index lags delivery by seconds to minutes, so a
message that has arrived is not necessarily findable — the exact class of loss
this section exists to prevent.

*Triage cannot be Gmail's own* ``IMPORTANT`` *marker.* Measured on this mailbox:
9,210 inbox messages, 7,807 of them unread, and 100% of one day's arrivals were
bulk marketing. A signal trained on a mailbox nobody reads is not a signal. The
real implementation is a model pass over the headers and the snippet — cheap,
because that is a few hundred tokens per message and only for messages the
history call already said are new.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from jarvis.briefing.sources import CandidateItem, Fetch, Scored, down, unconfigured

__all__ = [
    "GMAIL_CURSOR",
    "NOT_CONNECTED",
    "RESYNC_LIMIT",
    "FakeGmailApi",
    "GmailApi",
    "GmailSource",
    "HistoryGone",
    "KeywordTriage",
    "NoTriage",
    "UnconnectedInbox",
    "header_of",
    "message_item",
]

#: The row in ``cursors`` this source owns. Named in 001_init.sql's own comment,
#: so it is that name and not a new one.
GMAIL_CURSOR = "gmail_history_id"

#: Said out loud, verbatim, so it is written as a sentence rather than assembled
#: from a noun at the point of speaking.
NOT_CONNECTED = "Your inbox is not connected yet."

#: How many messages a full resync reads back. A briefing that says more than a
#: handful of things about an inbox is not a briefing, and the resync is the path
#: taken when the cursor is gone — i.e. exactly when reading everything would be
#: most expensive and least useful.
RESYNC_LIMIT = 50


class HistoryGone(LookupError):
    """``users.history.list`` answered 404: the stored historyId has aged out.

    Its own type because it is a BRANCH and not a failure. Caught as a generic
    error it becomes "your inbox looks quiet", which is a lie that gets told
    about once a week — Gmail keeps roughly seven days of history.
    """


class GmailApi(Protocol):
    """The two calls this section needs, and deliberately nothing else.

    Both return Gmail's own JSON, unflattened, because the day somebody needs a
    header this module currently ignores, they should not have to change two
    layers to get at it.
    """

    def history(self, start_history_id: str) -> Mapping[str, Any]:
        """``users.history.list`` from ``start_history_id``. Raises :class:`HistoryGone` on 404."""

    def recent(self, limit: int) -> Mapping[str, Any]:
        """Newest-first inbox metadata: the full-resync path. See the module docstring."""


def header_of(message: Mapping[str, Any], name: str) -> str:
    """One header value, case-insensitively, or ``""``.

    Gmail returns headers as an ARRAY of name/value objects with whatever casing
    the sending MTA used, so ``message["payload"]["headers"]["From"]`` — the
    thing everybody writes first — is a TypeError against real data.
    """
    wanted = name.casefold()
    payload = message.get("payload") or {}
    for header in payload.get("headers") or ():
        if str(header.get("name", "")).casefold() == wanted:
            return str(header.get("value", ""))
    return ""


def _internal_date(message: Mapping[str, Any]) -> str | None:
    """``internalDate`` (epoch MILLISECONDS, as a string) -> our UTC timestamp.

    Gmail's own delivery time rather than the ``Date:`` header, which is written
    by the sender and is wrong often enough to reorder a briefing.
    """
    raw = message.get("internalDate")
    if raw is None:
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    t = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.{t.microsecond // 1000:03d}Z"


def message_item(message: Mapping[str, Any]) -> CandidateItem:
    """One Gmail message as a candidate. The id is Gmail's, so the seen set is exact."""
    sender = header_of(message, "From") or "someone"
    subject = header_of(message, "Subject").strip() or "(no subject)"
    return CandidateItem(
        id=str(message.get("id", "")),
        line=f"{sender}: {subject}",
        at=_internal_date(message),
        detail=str(message.get("snippet", "")),
    )


@dataclass(frozen=True, slots=True)
class GmailSource:
    """History-based reads, with the resync branch taken rather than logged."""

    api: GmailApi
    name: str = GMAIL_CURSOR

    def fetch(self, cursor: str | None) -> Fetch:
        try:
            if cursor is None:
                return self._resync("no history id stored yet")
            try:
                return self._delta(cursor)
            except HistoryGone:
                # NOT an error. See the module docstring: roughly weekly, by
                # design, and the only correct response is to read the inbox.
                return self._resync("the stored history id had aged out")
        except Exception as exc:
            # Deliberately broad. A Google client raises from a stack this module
            # has never seen, and the one outcome that must not happen is the
            # whole briefing dying because the inbox did.
            return down(f"I could not read your inbox: {exc}")

    def _delta(self, cursor: str) -> Fetch:
        page = self.api.history(cursor)
        messages: list[Mapping[str, Any]] = []
        for record in page.get("history") or ():
            for added in record.get("messagesAdded") or ():
                message = added.get("message")
                if isinstance(message, Mapping):
                    messages.append(message)
        return Fetch(
            items=tuple(message_item(m) for m in messages),
            # Gmail's own id, never ours: the next call must resume from the
            # point Gmail thinks we reached, not from a timestamp we invented.
            next_cursor=str(page.get("historyId") or cursor),
            notes={"mode": "history"},
        )

    def _resync(self, why: str) -> Fetch:
        page = self.api.recent(RESYNC_LIMIT)
        messages = [m for m in page.get("messages") or () if isinstance(m, Mapping)]
        return Fetch(
            items=tuple(message_item(m) for m in messages),
            next_cursor=str(page.get("historyId")) if page.get("historyId") else None,
            notes={"mode": "resync", "why": why},
        )


@dataclass(frozen=True, slots=True)
class UnconnectedInbox:
    """The source this machine actually has: none.

    It exists so the briefing can be run end to end today and say the true thing
    — "your inbox is not connected yet" — instead of the false one, which is
    silence.
    """

    name: str = GMAIL_CURSOR

    def fetch(self, cursor: str | None) -> Fetch:
        return unconfigured(NOT_CONNECTED)


# ───────────────────────────── triage ─────────────────────────────

#: A header that all but settles it. Bulk senders are required to offer an
#: unsubscribe path, and a human writing to one person does not. This is the
#: measured cheap signal, not a model — and it is the reason the fake can be
#: deterministic at all.
BULK_HEADERS = ("list-unsubscribe", "list-id", "precedence")


@dataclass(frozen=True, slots=True)
class NoTriage:
    """Rank by arrival, score everything the same. What "triage is down" looks like.

    A briefing with this still works: it says what arrived, in order, and does
    not claim to know which matters.
    """

    def score(self, items: Sequence[CandidateItem]) -> tuple[Scored, ...]:
        return tuple(Scored(item=i, score=0.0, reason="not triaged") for i in items)


@dataclass(frozen=True, slots=True)
class KeywordTriage:
    """The DETERMINISTIC fake, so the seam has a test. Not the design.

    The real one is a model pass over headers and snippet (module docstring).
    This scores on the two signals that need no model — an unsubscribe header
    means bulk, a known correspondent means not — which is enough to prove that
    a ranked section, an unranked section and a section whose triage threw are
    three different, testable outcomes.
    """

    known: frozenset[str] = frozenset()
    keywords: tuple[str, ...] = ()
    bulk_penalty: float = 1.0
    headers_by_id: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def score(self, items: Sequence[CandidateItem]) -> tuple[Scored, ...]:
        scored = [self._one(i) for i in items]
        # Stable sort on the negated score: equal scores keep arrival order,
        # which is the only tie-break a listener can predict.
        scored.sort(key=lambda s: -s.score)
        return tuple(scored)

    def _one(self, item: CandidateItem) -> Scored:
        headers = {k.casefold(): v for k, v in (self.headers_by_id.get(item.id) or {}).items()}
        reasons: list[str] = []
        score = 0.0
        if any(h in headers for h in BULK_HEADERS):
            score -= self.bulk_penalty
            reasons.append("bulk mail")
        haystack = f"{item.line} {item.detail}".casefold()
        if any(name.casefold() in haystack for name in self.known):
            score += 2.0
            reasons.append("someone you hear from")
        hits = [k for k in self.keywords if k.casefold() in haystack]
        if hits:
            score += float(len(hits))
            reasons.append("mentions " + ", ".join(hits))
        return Scored(item=item, score=score, reason="; ".join(reasons) or "nothing stood out")


@dataclass(slots=True)
class FakeGmailApi:
    """Scripted Gmail. Every test in this package runs on it; no test can reach Google.

    ``history_pages`` are returned in order by :meth:`history`; a queued
    :class:`HistoryGone` (or any exception) is raised instead, which is how the
    resync branch gets exercised without a real 404.
    """

    history_pages: list[Any] = field(default_factory=list)
    recent_page: Mapping[str, Any] | Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def history(self, start_history_id: str) -> Mapping[str, Any]:
        self.calls.append(("history", start_history_id))
        if not self.history_pages:
            return {"history": [], "historyId": start_history_id}
        page = self.history_pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    def recent(self, limit: int) -> Mapping[str, Any]:
        self.calls.append(("recent", limit))
        if isinstance(self.recent_page, Exception):
            raise self.recent_page
        return self.recent_page or {"messages": [], "historyId": None}
