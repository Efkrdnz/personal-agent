"""Local or cloud: the answer is local, it is said out loud, and the asking is recorded.

Cloud mode is cut (ADR 0009) and the ADR's revisit condition is six months of
recorded phrasing — so the deliverable of this module is the RECORD, and the tests
are about what ends up in the activity log as much as about what is said.

The classifier itself is :func:`jarvis.spec.parse_mode`, tested there. What is
tested here is that this layer does not grow a second one, that every honest case
speaks, and that asking twice writes one row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import spec
from jarvis.bus import read_since
from jarvis.db import connect, migrate
from jarvis.project import mode


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def events_of(con: sqlite3.Connection, kind: str) -> list[dict[str, object]]:
    return [e.payload for e in read_since(con, 0) if e.kind == kind]


def test_the_classifier_is_the_spines_and_not_a_second_one() -> None:
    """A second regex would be a second answer, and the drifted one is never watched."""
    for said in ("build it in the cloud", "run it locally", "bulutta çalıştır", "yerelde"):
        assert mode.classify(said) == spec.parse_mode(said)


@pytest.mark.parametrize(
    ("utterance", "asked"),
    [
        ("let's build an app called comment watcher", "unspecified"),
        ("build it in the cloud", "cloud"),
        ("bulutta yapalım", "cloud"),
        ("so nothing touches my local disk", "cloud"),
        ("run it on my machine", "local"),
        ("yerelde kalsın", "local"),
        ("do it in the cloud, on this machine", "unclear"),
    ],
)
def test_what_the_phrasing_asked_for_is_recorded_verbatim(
    con: sqlite3.Connection, utterance: str, asked: str
) -> None:
    decision = mode.resolve(con, utterance=utterance, actor="desk")
    assert decision.asked == asked
    assert decision.mode == "local"

    recorded = events_of(con, mode.MODE_EVENT)
    assert len(recorded) == 1
    assert recorded[0]["asked"] == asked
    assert recorded[0]["utterance"] == utterance
    assert recorded[0]["mode"] == "local"


def test_asking_for_the_cloud_is_answered_honestly_rather_than_silently(
    con: sqlite3.Connection,
) -> None:
    decision = mode.resolve(con, utterance="build it in the cloud", actor="desk")
    assert decision.honest
    assert decision.spoken is not None
    assert "can only run locally" in decision.spoken
    assert "here on your machine" in decision.spoken
    # And the log holds the sentence that was actually said.
    assert events_of(con, mode.MODE_EVENT)[0]["said"] == decision.spoken


def test_ambiguous_phrasing_admits_it_could_not_tell(con: sqlite3.Connection) -> None:
    decision = mode.resolve(con, utterance="in the cloud please, on this machine", actor="desk")
    assert decision.asked == "unclear"
    assert decision.honest
    assert decision.spoken is not None
    assert "couldn't tell" in decision.spoken


def test_saying_nothing_about_it_says_nothing_back(con: sqlite3.Connection) -> None:
    """A note nobody needed is noise, and noise is what gets the useful notes ignored."""
    decision = mode.resolve(con, utterance="build a comment watcher", actor="desk")
    assert decision.asked == "unspecified"
    assert decision.spoken is None
    assert not decision.honest
    # Still recorded: "nobody asked for the cloud" is the finding the ADR wants.
    assert len(events_of(con, mode.MODE_EVENT)) == 1


def test_asking_for_local_is_confirmed_without_a_lecture(con: sqlite3.Connection) -> None:
    decision = mode.resolve(con, utterance="run it on my machine", actor="desk")
    assert decision.asked == "local"
    assert decision.spoken is None  # they asked for what they are getting


def test_the_same_sentence_classified_twice_is_one_row(con: sqlite3.Connection) -> None:
    """A restarted process must not inflate the phrasing count it exists to measure."""
    mode.resolve(con, utterance="build it in the cloud", actor="desk")
    mode.resolve(con, utterance="build it in the cloud", actor="desk")
    assert len(events_of(con, mode.MODE_EVENT)) == 1

    mode.resolve(con, utterance="build it in the cloud please", actor="desk")
    assert len(events_of(con, mode.MODE_EVENT)) == 2


def test_the_wording_is_the_spines_own_so_the_two_cannot_drift() -> None:
    """jarvis.spec already says this sentence in the spec flow; there is one copy."""
    ask = spec.parse_mode("build it in the cloud")
    assert mode.spoken_mode_line(ask) == spec.mode_note(
        spec.Spec(
            requirements=(),
            mode="local",
            model=None,
            effort=None,
            repo_name="",
            model_ask=spec.ModelAsk(alias=None, phrase=None),
            effort_ask=spec.EffortAsk(level=None, phrase=None),
            mode_ask=ask,
        )
    )
