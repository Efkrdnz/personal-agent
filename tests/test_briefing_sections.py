"""Composing one section: what it says, and what it refuses to say.

The rule under test throughout is that a section never lies by omission. A
source that is down, a source that is not connected, a source that raised
something nobody anticipated and a source that genuinely had nothing are four
different sentences, and three of them are not "nothing new".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jarvis import jobs, reconcile
from jarvis.briefing.sections import MAX_SPOKEN, Sources, compose, spec_for
from jarvis.briefing.sources import CandidateItem, Fetch, Scored
from jarvis.db import connect, migrate


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


@dataclass
class Scripted:
    """A source that answers with whatever the test put in it. Records the cursor."""

    name: str = "github_issues_since"
    answers: list[Fetch] = field(default_factory=list)
    seen_cursor: list[str | None] = field(default_factory=list)

    def fetch(self, cursor: str | None) -> Fetch:
        self.seen_cursor.append(cursor)
        return self.answers.pop(0) if self.answers else Fetch()


@dataclass
class Exploding:
    """A source written by somebody who did not read the protocol."""

    name: str = "github_issues_since"

    def fetch(self, cursor: str | None) -> Fetch:
        raise ZeroDivisionError("a stack nobody here has seen")


def items(n: int, start: int = 1) -> tuple[CandidateItem, ...]:
    return tuple(
        CandidateItem(id=f"issue:{i}", line=f"ada opened issue {i}", at=f"2026-09-1{i % 10}")
        for i in range(start, start + n)
    )


def test_a_section_says_how_many_and_then_says_them(con: sqlite3.Connection) -> None:
    source = Scripted(answers=[Fetch(items=items(2), next_cursor="2026-09-17T00:00:00Z")])
    section = compose(con, "issues", sources=Sources(issues=source), cursor="2026-09-16T00:00:00Z")

    assert section.ok
    assert section.lines[0] == "2 new issues on your repositories."
    assert section.lines[1:] == ("ada opened issue 1", "ada opened issue 2")
    assert section.item_ids == ("issue:1", "issue:2")
    assert section.next_cursor == "2026-09-17T00:00:00Z"
    assert source.seen_cursor == ["2026-09-16T00:00:00Z"]


def test_nothing_new_is_a_different_sentence_from_could_not_check(
    con: sqlite3.Connection,
) -> None:
    """The whole point. Both are short; only one of them means something is wrong."""
    quiet = compose(con, "issues", sources=Sources(issues=Scripted(answers=[Fetch()])))
    assert quiet.ok
    assert quiet.lines == ("No new issues on your repositories.",)

    broken = compose(
        con,
        "issues",
        sources=Sources(issues=Scripted(answers=[Fetch(ok=False, error="GitHub said 503.")])),
    )
    assert broken.ok is False
    assert broken.lines == ("GitHub said 503.",)
    # Nothing is remembered and no cursor moves, so tomorrow asks the same
    # question of the same cursor and nothing is lost by a bad morning.
    assert broken.item_ids == ()
    assert broken.next_cursor is None


def test_a_source_that_raises_cannot_take_the_briefing_down(con: sqlite3.Connection) -> None:
    section = compose(con, "issues", sources=Sources(issues=Exploding()))
    assert section.ok is False
    assert "could not check new issues" in section.lines[0]


def test_a_source_with_no_credential_says_it_is_not_connected(con: sqlite3.Connection) -> None:
    """Gmail and YouTube really are in this state: there are no Google credentials.

    EVERY remote section, not just the two whose titles happen to read as English.
    The earlier version of this test skipped "issues" and so did not notice that
    the sentence was assembled as ``f"{spec.title} is not connected yet"`` — which
    for that section came out, verbatim and out loud, as "new issues is not
    connected yet".
    """
    for key in ("inbox", "issues", "comments"):
        section = compose(con, key, sources=Sources())
        assert section.ok is False
        assert "not connected yet" in section.lines[0]
        # A sentence, not a fragment: capitalised and closed.
        assert section.lines[0][0].isupper() and section.lines[0].endswith(".")
        assert section.lines[0] == spec_for(key).not_connected


def test_an_item_already_said_is_not_said_again(con: sqlite3.Connection) -> None:
    """The cursors are inclusive on purpose; the seen set is what gives exactness."""
    source = Scripted(answers=[Fetch(items=items(3))])
    section = compose(con, "issues", sources=Sources(issues=source), seen={"issue:1", "issue:2"})
    assert section.lines[0] == "One new issue on your repositories."
    assert section.item_ids == ("issue:3",)


def test_everything_already_said_is_reported_as_nothing_new(con: sqlite3.Connection) -> None:
    source = Scripted(answers=[Fetch(items=items(2))])
    section = compose(con, "issues", sources=Sources(issues=source), seen={"issue:1", "issue:2"})
    assert section.lines == ("No new issues on your repositories.",)
    assert section.item_ids == ()


def test_the_tail_is_counted_out_loud_rather_than_dropped(con: sqlite3.Connection) -> None:
    source = Scripted(answers=[Fetch(items=items(MAX_SPOKEN + 3))])
    section = compose(con, "issues", sources=Sources(issues=source))

    assert len(section.lines) == MAX_SPOKEN + 2
    assert section.lines[-1] == "There are 3 more I haven't read out."
    # They are still REMEMBERED: a briefing that re-reads the same tail every
    # morning is the one people stop listening to, and the count was said.
    assert len(section.item_ids) == MAX_SPOKEN + 3


# ───────────────────────────── triage ─────────────────────────────


@dataclass
class ByLength:
    def score(self, candidates: Sequence[CandidateItem]) -> tuple[Scored, ...]:
        ranked = sorted(candidates, key=lambda i: -len(i.line))
        return tuple(Scored(item=i, score=float(len(i.line)), reason="length") for i in ranked)


@dataclass
class Losing:
    """Triage that drops an item. The failure that would silently lose mail."""

    def score(self, candidates: Sequence[CandidateItem]) -> tuple[Scored, ...]:
        return tuple(Scored(item=i, score=0.0) for i in candidates[:1])


@dataclass
class Throwing:
    def score(self, candidates: Sequence[CandidateItem]) -> tuple[Scored, ...]:
        raise RuntimeError("the model timed out")


def inbox_items() -> tuple[CandidateItem, ...]:
    return (
        CandidateItem(id="m1", line="short"),
        CandidateItem(id="m2", line="a much longer subject line"),
    )


def test_triage_decides_the_order_of_the_inbox(con: sqlite3.Connection) -> None:
    section = compose(
        con,
        "inbox",
        sources=Sources(
            inbox=Scripted(name="gmail_history_id", answers=[Fetch(items=inbox_items())]),
            triage=ByLength(),
        ),
    )
    assert section.lines[1:] == ("a much longer subject line", "short")
    assert section.item_ids == ("m2", "m1")


def test_triage_falling_over_leaves_the_section_unranked_and_says_so(
    con: sqlite3.Connection,
) -> None:
    """A model call WILL fail. The messages are still read out; the ranking is not faked."""
    section = compose(
        con,
        "inbox",
        sources=Sources(
            inbox=Scripted(name="gmail_history_id", answers=[Fetch(items=inbox_items())]),
            triage=Throwing(),
        ),
    )
    assert section.ok
    assert section.item_ids == ("m1", "m2")
    assert any("could not work out which of these matter" in line for line in section.lines)


def test_a_triage_that_drops_an_item_has_its_ordering_refused(con: sqlite3.Connection) -> None:
    section = compose(
        con,
        "inbox",
        sources=Sources(
            inbox=Scripted(name="gmail_history_id", answers=[Fetch(items=inbox_items())]),
            triage=Losing(),
        ),
    )
    assert section.item_ids == ("m1", "m2")
    assert any("could not work out" in line for line in section.lines)


def test_only_the_inbox_is_triaged(con: sqlite3.Connection) -> None:
    """Issues and comments arrive in time order and must stay in it."""
    source = Scripted(answers=[Fetch(items=items(2))])
    section = compose(con, "issues", sources=Sources(issues=source, triage=ByLength()))
    assert section.item_ids == ("issue:1", "issue:2")


# ───────────────────────────── section one ─────────────────────────────


def test_project_status_keeps_standing_lines_and_dedupes_events(
    con: sqlite3.Connection,
) -> None:
    done = jobs.create_job(
        con, kind="claude_code", title="the todo app build", created_by="desk", state="running"
    )
    jobs.set_state(con, done.id, "done", result_summary="Seventeen tests pass.")

    first = compose(con, "projects")
    assert any("the todo app build finished." in line for line in first.lines)
    assert first.item_ids == (f"job:{done.id}:done",)

    # Said once already: the event goes, the section's own "nothing new" sentence
    # arrives — which is NOT reconcile's "nothing has finished or failed", because
    # those are different facts.
    again = compose(con, "projects", seen=set(first.item_ids))
    assert again.lines == ("Nothing new about your projects since I last told you.",)
    assert again.item_ids == ()
    assert again.cursor_name == reconcile.BRIEFING_CURSOR
