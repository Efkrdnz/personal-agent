"""Cursor discipline: no unbounded repetition, and no silent loss.

This is the stage's exit criterion, and it is deliberately NOT "no repetition".
:func:`jarvis.reconcile.set_briefing_cursor` documents the decision every cursor
here follows: they are INCLUSIVE, because hearing "the scraper build failed"
twice is annoying while never hearing it is the failure the briefing exists to
prevent. Exactness comes from the ``briefing_seen`` set on top, which is cheap.

The other half is when a cursor may move at all: only after a section really
reached a human. A briefing composed into an empty room, a section whose source
was down, and a section the user skipped all leave their cursors exactly where
they were.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jarvis import answers as ans
from jarvis import jobs, reconcile
from jarvis import requests as rq
from jarvis.briefing import navigator as nav
from jarvis.briefing import store
from jarvis.briefing.sections import Sources
from jarvis.briefing.sources import CandidateItem, Fetch
from jarvis.db import connect, migrate

ISSUES = "github_issues_since"
GMAIL = "gmail_history_id"


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


@dataclass
class Mailbox:
    """A source that behaves like the real ones: ``at >= cursor``, INCLUSIVE.

    The boundary item therefore comes back on the next run, exactly as GitHub's
    ``created:>=`` and YouTube's newest-first paging do. If the briefing were
    relying on the cursor alone for exactness, this fake would expose it.
    """

    name: str = ISSUES
    have: list[CandidateItem] = field(default_factory=list)
    asked: list[str | None] = field(default_factory=list)

    def fetch(self, cursor: str | None) -> Fetch:
        self.asked.append(cursor)
        hits = [i for i in self.have if cursor is None or (i.at or "") >= cursor]
        stamps = [i.at for i in hits if i.at]
        return Fetch(items=tuple(hits), next_cursor=max(stamps) if stamps else None)


def an_issue(n: int, at: str) -> CandidateItem:
    return CandidateItem(id=f"issue:{n}", line=f"ada opened issue {n}", at=at)


def say_the_section(con: sqlite3.Connection, briefing_id: str, sources: Sources, label: str):
    """Deliver the section at the pointer and answer it, the way a channel would."""
    offer = nav.deliver(con, briefing_id, sources=sources)
    assert offer is not None
    index = next(i["index"] for i in offer.request.presentation["items"] if i["label"] == label)
    assert rq.answer_request(
        con, offer.request.id, ans.answer(offer.request.payload, [index]), "desk", "voice"
    )
    nav.apply_answer(con, offer.request.id)
    return offer


def run_the_issues_section(
    con: sqlite3.Connection, run_key: str, box: Mailbox, label: str = "Done"
):
    """One morning, reduced to the one section under test.

    A one-section briefing offers "Done" rather than "Next": every option is
    reachable, and there is nothing after this one to go on to.
    """
    briefing = nav.begin(con, keys=("issues",), run_key=run_key)
    return say_the_section(con, briefing.id, Sources(issues=box), label)


# ───────────────────────── across two mornings ─────────────────────────


def test_no_item_is_said_twice_across_two_runs(con: sqlite3.Connection) -> None:
    """The exit criterion, with a source whose cursor really is inclusive."""
    box = Mailbox(have=[an_issue(1, "2026-09-16T08:00:00Z"), an_issue(2, "2026-09-17T06:00:00Z")])
    monday = run_the_issues_section(con, "2026-09-17", box)
    assert [line for line in monday.content.lines[1:]] == [
        "ada opened issue 1",
        "ada opened issue 2",
    ]
    assert store.cursor_of(con, ISSUES) == "2026-09-17T06:00:00Z"

    # Overnight: one new issue, and the boundary one the inclusive cursor
    # inevitably hands back a second time.
    box.have.append(an_issue(3, "2026-09-18T05:00:00Z"))
    tuesday = run_the_issues_section(con, "2026-09-18", box)

    assert box.asked[-1] == "2026-09-17T06:00:00Z"
    assert tuesday.content.lines == (
        "One new issue on your repositories.",
        "ada opened issue 3",
    )
    assert "ada opened issue 2" not in tuesday.content.spoken


def test_a_quiet_second_morning_says_nothing_new_rather_than_repeating(
    con: sqlite3.Connection,
) -> None:
    box = Mailbox(have=[an_issue(1, "2026-09-17T06:00:00Z")])
    run_the_issues_section(con, "2026-09-17", box)
    tuesday = run_the_issues_section(con, "2026-09-18", box)
    assert tuesday.content.lines == ("No new issues on your repositories.",)


# ───────────────────────── when a cursor may move ─────────────────────────


def test_a_briefing_composed_into_an_empty_room_is_said_again(
    con: sqlite3.Connection,
) -> None:
    """Composing proves the machine was awake. Only an answer proves a human heard it."""
    box = Mailbox(have=[an_issue(1, "2026-09-17T06:00:00Z")])
    briefing = nav.begin(con, keys=("issues",), run_key="2026-09-17")
    offer = nav.deliver(con, briefing.id, sources=Sources(issues=box))
    assert offer is not None

    assert store.cursor_of(con, ISSUES) is None
    assert store.seen_ids(con, ISSUES) == set()

    # Nobody ever answered. Tomorrow says it again, in full.
    tomorrow = run_the_issues_section(con, "2026-09-18", box)
    assert "ada opened issue 1" in tomorrow.content.spoken


def test_a_section_whose_source_was_down_costs_nothing(con: sqlite3.Connection) -> None:
    """A bad morning must not eat a day's issues."""

    @dataclass
    class Broken:
        name: str = ISSUES

        def fetch(self, cursor: str | None) -> Fetch:
            return Fetch(ok=False, error="GitHub said 503.")

    monday = run_the_issues_section(con, "2026-09-17", Broken())  # type: ignore[arg-type]
    assert monday.content.ok is False
    assert store.cursor_of(con, ISSUES) is None
    assert store.seen_ids(con, ISSUES) == set()

    box = Mailbox(have=[an_issue(1, "2026-09-16T23:00:00Z")])
    tuesday = run_the_issues_section(con, "2026-09-18", box)
    assert "ada opened issue 1" in tuesday.content.spoken


def test_a_skipped_section_keeps_its_cursor(con: sqlite3.Connection) -> None:
    """ "Skip" must not quietly become "discard"."""
    box = Mailbox(name="gmail_history_id", have=[an_issue(1, "2026-09-17T06:00:00Z")])
    briefing = nav.begin(con, keys=("projects", "inbox", "issues"), run_key="2026-09-17")
    say_the_section(con, briefing.id, Sources(inbox=box), "Skip to new issues")

    assert store.cursor_of(con, "gmail_history_id") is None
    assert store.seen_ids(con, "gmail_history_id") == set()
    rows = {s.key: s.state for s in store.sections(con, briefing.id)}
    assert rows["inbox"] == "skipped"


def test_the_projects_cursor_is_written_by_reconcile_and_not_by_a_second_writer(
    con: sqlite3.Connection,
) -> None:
    """One decision, one writer. The INCLUSIVE choice lives in jarvis.reconcile."""
    done = jobs.create_job(
        con, kind="claude_code", title="the todo app build", created_by="desk", state="running"
    )
    jobs.set_state(con, done.id, "done")
    briefing = nav.begin(con, keys=("projects",), run_key="2026-09-17")
    offer = say_the_section(con, briefing.id, Sources(), "Done")

    assert reconcile.briefing_cursor(con) == offer.content.next_cursor
    assert store.cursor_of(con, reconcile.BRIEFING_CURSOR) == reconcile.briefing_cursor(con)
    assert store.seen_ids(con, reconcile.BRIEFING_CURSOR) == {f"job:{done.id}:done"}


# ───────────────────────── the mechanics underneath ─────────────────────────


def test_a_cursor_never_goes_backwards(con: sqlite3.Connection) -> None:
    """A lagging search index or an older resync page must not widen the window.

    Storing an earlier value would re-read a range whose items are all in the
    seen set already: harmless once, and an ever-growing query after that.
    """
    store.advance_cursor(con, ISSUES, "2026-09-17T06:00:00Z")
    assert store.advance_cursor(con, ISSUES, "2026-09-10T06:00:00Z") == "2026-09-17T06:00:00Z"
    assert store.cursor_of(con, ISSUES) == "2026-09-17T06:00:00Z"


def test_remembering_is_idempotent_and_scoped_to_one_source(con: sqlite3.Connection) -> None:
    assert store.remember(con, ISSUES, ("a", "b")) == 2
    assert store.remember(con, ISSUES, ("b", "c")) == 1
    assert store.seen_ids(con, ISSUES, ("a", "b", "c", "d")) == {"a", "b", "c"}
    # Two sources cannot suppress each other's items by id collision.
    assert store.seen_ids(con, "youtube_page", ("a",)) == set()


def test_the_exactness_set_can_be_pruned(con: sqlite3.Connection) -> None:
    store.remember(con, ISSUES, ("old",), now_ts="2026-08-01T00:00:00.000Z")
    store.remember(con, ISSUES, ("new",), now_ts="2026-09-17T00:00:00.000Z")
    assert store.forget_older_than(con, "2026-09-01T00:00:00.000Z") == 1
    assert store.seen_ids(con, ISSUES) == {"new"}


def test_a_gmail_history_id_is_counted_as_a_number_and_not_as_text(
    con: sqlite3.Connection,
) -> None:
    """The one cursor in this system that is not a timestamp.

    Gmail's ``historyId`` is a decimal integer that grows without bound, so it
    sorts as TEXT in the wrong order the morning it gains a digit: ``"10000001"
    <= "9999999"`` is true. The monotonic guard then calls every future id older
    than the stored one and pins the cursor there forever — the delta re-reads a
    widening window every day until the id ages out, and after that every morning
    is a full resync. Nothing about that is visible in the briefing, which is
    exactly why it needs a test.
    """
    assert store.advance_cursor(con, GMAIL, "9999999") == "9999999"
    assert store.advance_cursor(con, GMAIL, "10000001") == "10000001"
    assert store.cursor_of(con, GMAIL) == "10000001"
    # And still monotonic, numerically: an older page cannot widen the window.
    assert store.advance_cursor(con, GMAIL, "9999999") == "10000001"
