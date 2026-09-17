"""A briefing section IS a request, and this file is where that claim is falsifiable.

If it holds, three things are true and each has a test here:

* "next" is an ANSWER, resolved by the numbering and the answer shapes the spine
  already has. Both existing channels — the desk's option numbering
  (:mod:`jarvis.answers`) and Telegram's keyboard
  (:func:`jarvis.telegram.channel.build_answer`) — produce navigation without a
  single line of briefing-specific parsing.
* the pointer is server-side, so a briefing delivered on one connection and
  dropped resumes on another AT SECTION THREE.
* there is no second confirmation mechanism: the section's request goes through
  ``create_request`` / ``answer_request`` / ``consume`` like every other human
  decision in the system.

The failure this file is watching for is a second navigation mechanism appearing
in :mod:`jarvis.briefing.navigator`. If one ever does, the claim was false and
saying so is worth more than the code.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jarvis import answers as ans
from jarvis import requests as rq
from jarvis.briefing import navigator as nav
from jarvis.briefing import store
from jarvis.briefing.sections import SECTION_KEYS, Sources
from jarvis.briefing.sources import CandidateItem, Fetch
from jarvis.db import connect, migrate
from jarvis.telegram import channel as tg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jarvis.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


@pytest.fixture
def con(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def other(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A DIFFERENT connection: the channel that attaches after the first one dropped."""
    c = connect(db_path)
    yield c
    c.close()


@dataclass
class Scripted:
    name: str
    answers: list[Fetch] = field(default_factory=list)

    def fetch(self, cursor: str | None) -> Fetch:
        return self.answers.pop(0) if self.answers else Fetch()


def a_source(name: str, *lines: str) -> Scripted:
    return Scripted(
        name=name,
        answers=[
            Fetch(
                items=tuple(
                    CandidateItem(id=f"{name}:{i}", line=line, at="2026-09-17T06:00:00Z")
                    for i, line in enumerate(lines, start=1)
                ),
                next_cursor="2026-09-17T06:00:00Z",
            )
        ],
    )


def all_sources() -> Sources:
    return Sources(
        inbox=a_source("gmail_history_id", "ada: about Thursday"),
        issues=a_source("github_issues_since", "ada opened issue 7"),
        comments=a_source("youtube_page", "bob commented: nice video"),
    )


def label_index(req: rq.Request, label: str) -> int:
    return next(i["index"] for i in req.presentation["items"] if i["label"] == label)


def answer_as_desk(con: sqlite3.Connection, req: rq.Request, label: str) -> None:
    """The desk path: an option INDEX through the spine's own numbering."""
    answer = ans.answer(req.payload, [label_index(req, label)])
    assert rq.answer_request(con, req.id, answer, "desk", "voice")


def answer_as_telegram(con: sqlite3.Connection, req: rq.Request, label: str) -> None:
    """The Telegram path: the channel that already exists, untouched by this stage."""
    answer = tg.build_answer(req, picks=(label_index(req, label),))
    assert rq.answer_request(con, req.id, answer, "telegram", "button")


# ───────────────────────── a section is a request ─────────────────────────


def test_a_section_is_one_row_in_requests(con: sqlite3.Connection) -> None:
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None

    req = rq.get_request(con, offer.request.id)
    assert req is not None
    assert req.kind == "briefing_gate"
    assert req.state == "pending"
    # Verbatim: these lines name jobs and senders read out of rows.
    assert req.presentation["verbatim"] is True
    assert req.presentation["question"] == nav.NAV_QUESTION
    labels = [i["label"] for i in req.presentation["items"]]
    assert labels[0] == "Next"
    assert "Repeat that" in labels and "Stop" in labels
    # Reachable options only: the section AFTER the next one is a skip target,
    # the next one is simply "Next".
    assert "Skip to new issues" in labels
    assert "Skip to your inbox" not in labels


def test_the_desk_numbering_produces_a_navigation_command(con: sqlite3.Connection) -> None:
    """The claim, stated as a test: neither channel knows what a briefing is."""
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    answer_as_desk(con, offer.request, "Next")

    move = nav.apply_answer(con, offer.request.id)
    assert move is not None
    assert move.command == "next"
    assert move.position == 1
    assert store.get(con, briefing.id).position == 1  # type: ignore[union-attr]


def test_a_telegram_tap_produces_the_same_command(con: sqlite3.Connection) -> None:
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    answer_as_telegram(con, offer.request, "Next")

    move = nav.apply_answer(con, offer.request.id)
    assert move is not None and move.command == "next"


# ───────────────────────── the pointer is server-side ─────────────────────────


def test_a_dropped_channel_resumes_at_section_three(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """THE test for this stage. Two sections on one connection, the third on another.

    ``other`` is a different ``sqlite3.Connection`` on the same file — the desk
    process having died and a Telegram bot picking the briefing up, as far as the
    database can tell. Nothing about where the briefing had got to lived in the
    first process.
    """
    briefing = nav.begin(con)

    first = nav.deliver(con, briefing.id, sources=all_sources())
    assert first is not None and first.content.key == "projects"
    answer_as_desk(con, first.request, "Next")
    nav.apply_answer(con, first.request.id)

    second = nav.deliver(con, briefing.id, sources=all_sources())
    assert second is not None and second.content.key == "inbox"
    answer_as_desk(con, second.request, "Next")
    nav.apply_answer(con, second.request.id)

    # …and here the channel goes away mid-briefing.
    third = nav.deliver(other, briefing.id, sources=all_sources())
    assert third is not None
    assert third.position == 2
    assert third.content.key == "issues"
    assert "ada opened issue 7" in third.content.spoken


def test_an_answer_nobody_applied_is_applied_by_whoever_attaches_next(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The channel dropped between the tap and the handling. Nothing is lost."""
    briefing = nav.begin(con)
    first = nav.deliver(con, briefing.id, sources=all_sources())
    assert first is not None
    answer_as_telegram(con, first.request, "Next")
    # No apply_answer here: the process that would have done it is gone.

    resumed = nav.deliver(other, briefing.id, sources=all_sources())
    assert resumed is not None
    assert resumed.content.key == "inbox"
    assert store.get(other, briefing.id).position == 1  # type: ignore[union-attr]


def test_a_second_channel_gets_the_same_pending_question(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Two devices must not ask the same section twice; the row is the same row."""
    briefing = nav.begin(con)
    first = nav.deliver(con, briefing.id, sources=all_sources())
    again = nav.deliver(other, briefing.id, sources=all_sources())
    assert first is not None and again is not None
    assert again.request.id == first.request.id
    assert again.resumed is True


def test_two_processes_starting_at_once_on_two_connections_make_one_briefing(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Two real connections, one file — the supervisor restarted the desk twice.

    Same as the rest of this system: the insert takes the write lock before it
    looks, so the loser finds the row rather than creating a rival with its own
    pointer. Two pointers would be two voices reading different sections at the
    same person.
    """
    mine = nav.begin(con, run_key="2026-09-17")
    theirs = nav.begin(other, run_key="2026-09-17")
    assert mine.id == theirs.id
    assert con.execute("SELECT count(*) FROM briefings").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM briefing_sections").fetchone()[0] == len(SECTION_KEYS)


def test_two_channels_applying_one_answer_move_the_pointer_once(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """The desk and the Telegram bot both notice the same answer land.

    Both call :func:`apply_answer` because neither knows the other exists. The
    section must be committed once — one delivered event, one cursor move, one
    step of the pointer — and the second caller must not be told it failed
    either, because there is nothing for it to retry.
    """
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    answer_as_desk(con, offer.request, "Next")

    first = nav.apply_answer(con, offer.request.id)
    second = nav.apply_answer(other, offer.request.id)
    assert first is not None and second is not None
    assert (first.command, first.position) == ("next", 1)
    assert (second.command, second.position) == ("next", 1)

    assert store.get(con, briefing.id).position == 1  # type: ignore[union-attr]
    delivered = con.execute(
        "SELECT count(*) FROM events WHERE kind='briefing.section.delivered'"
    ).fetchone()[0]
    assert delivered == 1


def test_starting_twice_in_one_morning_is_one_briefing(con: sqlite3.Connection) -> None:
    """The machine rebooted at 09:50 and came back up. One pointer, not two."""
    first = nav.begin(con, run_key="2026-09-17")
    second = nav.begin(con, run_key="2026-09-17")
    assert second.id == first.id
    assert len(store.sections(con, first.id)) == len(SECTION_KEYS)


# ───────────────────────── the four commands ─────────────────────────


def test_repeat_says_the_same_words_again(con: sqlite3.Connection) -> None:
    """A source read a second time could say something different. The stored text wins."""
    briefing = nav.begin(con)
    first = nav.deliver(con, briefing.id, sources=all_sources())
    assert first is not None
    answer_as_desk(con, first.request, "Repeat that")
    move = nav.apply_answer(con, first.request.id)
    assert move is not None and move.command == "repeat"
    assert move.position == 0

    # Empty Sources: if this re-composed, the section would now say "not
    # connected" instead of what the user heard a moment ago.
    again = nav.deliver(con, briefing.id, sources=Sources())
    assert again is not None
    assert again.content.spoken == first.content.spoken
    # A genuine re-ask, so a NEW attempt rather than the answered row.
    assert again.request.id != first.request.id
    assert again.request.attempt == first.request.attempt + 1


def test_skip_jumps_ahead_and_marks_what_it_jumped_over(con: sqlite3.Connection) -> None:
    briefing = nav.begin(con)
    first = nav.deliver(con, briefing.id, sources=all_sources())
    assert first is not None
    answer_as_desk(con, first.request, "Skip to new issues")
    move = nav.apply_answer(con, first.request.id)

    assert move is not None
    assert (move.command, move.target, move.position) == ("skip", "issues", 2)
    rows = {s.key: s for s in store.sections(con, briefing.id)}
    assert rows["inbox"].state == "skipped"
    # A skipped section was NOT heard, so nothing it would have settled is settled.
    assert store.cursor_of(con, "gmail_history_id") is None


def test_stop_ends_the_briefing_and_deliver_says_it_is_over(con: sqlite3.Connection) -> None:
    briefing = nav.begin(con)
    first = nav.deliver(con, briefing.id, sources=all_sources())
    assert first is not None
    answer_as_desk(con, first.request, "Stop")
    move = nav.apply_answer(con, first.request.id)

    assert move is not None and move.command == "stop" and move.finished
    assert store.get(con, briefing.id).state == "stopped"  # type: ignore[union-attr]
    assert nav.deliver(con, briefing.id, sources=all_sources()) is None


def test_the_last_section_ends_the_briefing(con: sqlite3.Connection) -> None:
    briefing = nav.begin(con, keys=("projects",))
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    assert [i["label"] for i in offer.request.presentation["items"]][0] == "Done"

    answer_as_desk(con, offer.request, "Done")
    move = nav.apply_answer(con, offer.request.id)
    assert move is not None and move.finished
    assert nav.deliver(con, briefing.id, sources=all_sources()) is None


# ───────────────────────── refusing to guess ─────────────────────────


def test_words_of_their_own_are_matched_against_the_offered_labels_only(
    con: sqlite3.Connection,
) -> None:
    """A lookup against the frozen array — never a parser, so it can only ever
    return an option that was actually offered."""
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None

    command, target, said = nav.command_of(offer.request, {"text": "next."})
    assert (command, target) == ("next", None)
    assert said == "next."


def test_an_answer_that_is_not_an_offered_option_is_unclear_not_a_guess(
    con: sqlite3.Connection,
) -> None:
    """Guessing "next" here would skip the section the user was asking about."""
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    assert rq.answer_request(
        con, offer.request.id, {"text": "what did the second one say?"}, "desk", "voice"
    )

    move = nav.apply_answer(con, offer.request.id)
    assert move is not None
    assert move.command == "unclear"
    assert move.position == 0
    assert store.get(con, briefing.id).position == 0  # type: ignore[union-attr]


def test_applying_the_same_answer_twice_moves_the_pointer_once(
    con: sqlite3.Connection, other: sqlite3.Connection
) -> None:
    """Two processes pick up the same poke. The compare-and-swap decides."""
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    answer_as_desk(con, offer.request, "Next")

    first = nav.apply_answer(con, offer.request.id)
    second = nav.apply_answer(other, offer.request.id)
    assert first is not None and second is not None
    assert store.get(con, briefing.id).position == 1  # type: ignore[union-attr]


def test_applying_a_still_pending_question_does_nothing(con: sqlite3.Connection) -> None:
    briefing = nav.begin(con)
    offer = nav.deliver(con, briefing.id, sources=all_sources())
    assert offer is not None
    assert nav.apply_answer(con, offer.request.id) is None
    assert store.get(con, briefing.id).position == 0  # type: ignore[union-attr]


def test_an_unknown_briefing_raises_rather_than_inventing_one(con: sqlite3.Connection) -> None:
    with pytest.raises(nav.UnknownBriefing):
        nav.deliver(con, "brf_nope", sources=all_sources())


def test_a_whole_briefing_runs_with_the_real_source_classes(con: sqlite3.Connection) -> None:
    """End to end on the shipped sources, in the state this machine is actually in.

    Gmail and YouTube have no credential, GitHub has a fake transport, and the
    briefing still delivers four sections — two of which say, out loud, that they
    could not be read. That is the difference between a briefing and a silence.
    """
    from jarvis.briefing.sections import Sources as RealSources
    from jarvis.briefing.sources.github import IssueSource, search_path
    from jarvis.briefing.sources.gmail import UnconnectedInbox
    from jarvis.briefing.sources.youtube import UnconnectedChannel
    from jarvis.github.transport import FakeTransport

    t = FakeTransport(login="Efkrdnz")
    t.script[f"GET {search_path('Efkrdnz', None)}"] = [{"total_count": 0, "items": []}]
    sources = RealSources(
        inbox=UnconnectedInbox(),
        issues=IssueSource(t, "Efkrdnz"),
        comments=UnconnectedChannel(),
    )

    briefing = nav.begin(con)
    said: list[str] = []
    while (offer := nav.deliver(con, briefing.id, sources=sources)) is not None:
        said.append(offer.content.spoken)
        label = "Done" if offer.position == len(SECTION_KEYS) - 1 else "Next"
        answer_as_desk(con, offer.request, label)
        nav.apply_answer(con, offer.request.id)

    assert len(said) == len(SECTION_KEYS)
    assert "Your inbox is not connected yet." in said[1]
    assert "No new issues on your repositories." in said[2]
    assert "Your YouTube channel is not connected yet." in said[3]
    assert store.get(con, briefing.id).state == "finished"  # type: ignore[union-attr]
    # A section that could not be read settles nothing.
    assert store.cursor_of(con, "gmail_history_id") is None
