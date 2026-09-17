"""The four data sources, and the one thing every one of them has to get right.

"No new issues" and "I could not check for issues" are different statements, and
only the second one tells the user something is broken. So most of this file is
failure: a Gmail history id that aged out, a GitHub token rejected overnight, a
YouTube quota that is spent, and a source that raises something nobody
anticipated. In every case the section must still be composable and must say
what happened.

Nothing here can reach Google or GitHub. Gmail and YouTube are protocols with
scripted fakes, and GitHub runs on the transport fake stage 4 already built.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis import jobs, reconcile
from jarvis.briefing.sources import CandidateItem
from jarvis.briefing.sources import projects as projects_source
from jarvis.briefing.sources.github import GITHUB_CURSOR, IssueSource, search_path
from jarvis.briefing.sources.gmail import (
    GMAIL_CURSOR,
    FakeGmailApi,
    GmailSource,
    HistoryGone,
    KeywordTriage,
    UnconnectedInbox,
    header_of,
)
from jarvis.briefing.sources.youtube import (
    YOUTUBE_CURSOR,
    CommentSource,
    FakeYoutubeApi,
    QuotaExceeded,
    UnconnectedChannel,
)
from jarvis.db import connect, migrate
from jarvis.github.transport import FakeTransport, TransportError, Unauthorized

LOGIN = "Efkrdnz"


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "jarvis.db")
    migrate(c)
    yield c
    c.close()


def a_message(
    mid: str, sender: str, subject: str, ms: int = 1_700_000_000_000
) -> dict[str, object]:
    return {
        "id": mid,
        "internalDate": str(ms),
        "snippet": "a snippet",
        "payload": {
            "headers": [{"name": "From", "value": sender}, {"name": "subject", "value": subject}]
        },
    }


# ───────────────────────── section one: projects ─────────────────────────


def test_section_one_uses_the_lines_reconcile_already_writes(con: sqlite3.Connection) -> None:
    """No second composer. The sentences come from jarvis.reconcile, verbatim."""
    done = jobs.create_job(
        con, kind="claude_code", title="the todo app build", created_by="desk", state="running"
    )
    jobs.set_state(con, done.id, "done", result_summary="Seventeen tests pass.")

    got = projects_source.fetch(con, None)
    status = reconcile.project_status(con)

    assert got.ok
    assert [i.line for i in got.items] == [status.finished[0].line]
    assert got.items[0].id == f"job:{done.id}:done"
    assert projects_source.PROJECTS_CURSOR == reconcile.BRIEFING_CURSOR


def test_a_standing_fact_is_not_an_item(con: sqlite3.Connection) -> None:
    """ "Still waiting for an answer" must be said EVERY morning until it stops being true.

    Only events go in the seen set. A standing fact that got deduped would mean a
    question raised on Monday going unmentioned for the rest of the week.
    """
    stuck = jobs.create_job(
        con, kind="claude_code", title="the api", created_by="desk", state="running"
    )
    req = _a_request(con, stuck.id)
    jobs.mark_blocked(con, stuck.id, req.id)
    con.execute(
        "UPDATE jobs SET blocked_since=? WHERE id=?",
        (jobs.shift_ts(reconcile.now(), -7500), stuck.id),
    )

    got = projects_source.fetch(con, None)
    assert got.items == ()
    assert any("the api has been waiting" in line for line in got.standing)


def _a_request(con: sqlite3.Connection, job_id: str) -> object:
    from jarvis import requests as rq

    return rq.create_request(
        con,
        kind="plan_question",
        short_label="the database",
        presentation=rq.make_presentation(intro="Which store?", options=["SQLite"]),
        payload={"questions": [{"question": "Which store?", "options": [{"label": "SQLite"}]}]},
        actor="test",
        job_id=job_id,
    )


# ───────────────────────── section two: the inbox ─────────────────────────


def test_a_header_is_found_whatever_case_the_sender_used() -> None:
    """Gmail returns headers as an array with the MTA's own casing, not a dict."""
    msg = a_message("m1", "ada@example.com", "the invoice")
    assert header_of(msg, "From") == "ada@example.com"
    assert header_of(msg, "Subject") == "the invoice"
    assert header_of(msg, "Reply-To") == ""


def test_the_inbox_reads_the_history_delta_and_stores_gmails_own_id() -> None:
    api = FakeGmailApi(
        history_pages=[
            {
                "history": [{"messagesAdded": [{"message": a_message("m1", "ada", "hello")}]}],
                "historyId": "2200",
            }
        ]
    )
    got = GmailSource(api).fetch("2100")

    assert got.ok
    assert [i.id for i in got.items] == ["m1"]
    # Gmail's id, never a timestamp of ours: the next read must resume where
    # Gmail thinks we got to.
    assert got.next_cursor == "2200"
    assert api.calls == [("history", "2100")]


def test_an_aged_out_history_id_resyncs_instead_of_reporting_an_empty_inbox() -> None:
    """Gmail keeps about a week of history. This is a BRANCH, not a failure.

    Caught as a generic error it becomes "your inbox looks quiet", which is a lie
    told roughly weekly.
    """
    api = FakeGmailApi(
        history_pages=[HistoryGone("404")],
        recent_page={"messages": [a_message("m9", "ada", "hello")], "historyId": "9000"},
    )
    got = GmailSource(api).fetch("1")

    assert got.ok
    assert [i.id for i in got.items] == ["m9"]
    assert got.next_cursor == "9000"
    assert got.notes["mode"] == "resync"
    assert ("recent", 50) in api.calls


def test_a_gmail_outage_says_so_rather_than_saying_nothing() -> None:
    api = FakeGmailApi(history_pages=[RuntimeError("500 backend error")])
    got = GmailSource(api).fetch("2100")

    assert got.ok is False
    assert got.items == ()
    assert got.next_cursor is None
    assert "could not read your inbox" in (got.error or "")


def test_an_inbox_with_no_credential_says_it_is_not_connected() -> None:
    got = UnconnectedInbox().fetch(None)
    assert got.ok is False
    assert "not connected" in (got.error or "")
    assert UnconnectedInbox().name == GMAIL_CURSOR


def test_triage_ranks_and_says_why() -> None:
    """The deterministic fake. The real one is a model pass; the SEAM is the point."""
    bulk = CandidateItem(id="m1", line="Shop: 70% off everything", detail="")
    real = CandidateItem(id="m2", line="ada: about Thursday", detail="are we still on")
    triage = KeywordTriage(
        known=frozenset({"ada"}),
        headers_by_id={"m1": {"List-Unsubscribe": "<mailto:x>"}},
    )
    scored = triage.score([bulk, real])

    assert [s.item.id for s in scored] == ["m2", "m1"]
    assert "bulk mail" in scored[1].reason
    assert "someone you hear from" in scored[0].reason


# ───────────────────────── section three: issues ─────────────────────────


def test_issues_are_one_search_call_against_the_other_rate_limit_bucket() -> None:
    """One request, not one per repository. Search is its own 30-a-minute budget."""
    path = search_path(LOGIN, "2026-09-16T07:00:00.000Z")
    t = FakeTransport(login=LOGIN)
    t.script[f"GET {path}"] = [
        {
            "total_count": 1,
            "items": [
                {
                    "id": 4242,
                    "number": 7,
                    "title": "the scraper drops rows",
                    "created_at": "2026-09-17T06:12:00Z",
                    "user": {"login": "ada"},
                    "repository_url": "https://api.github.com/repos/Efkrdnz/comment-watcher",
                    "html_url": "https://github.com/Efkrdnz/comment-watcher/issues/7",
                }
            ],
        }
    ]

    got = IssueSource(t, LOGIN).fetch("2026-09-16T07:00:00.000Z")

    assert len(t.sent("GET")) == 1
    assert t.writes == []
    assert "user%3AEfkrdnz" in path and "is%3Aissue" in path
    # Seconds precision: a query carrying milliseconds is silently ignored.
    assert "created%3A%3E%3D2026-09-16T07%3A00%3A00Z" in path
    assert got.ok
    assert got.items[0].id == "issue:4242"
    assert "ada opened Efkrdnz/comment-watcher issue 7" in got.items[0].line
    assert got.next_cursor == "2026-09-17T06:12:00Z"


def test_an_expired_github_token_is_reported_not_swallowed() -> None:
    t = FakeTransport(login=LOGIN)
    t.script[f"GET {search_path(LOGIN, None)}"] = [Unauthorized(401, "Bad credentials")]
    got = IssueSource(t, LOGIN).fetch(None)

    assert got.ok is False
    assert "token was rejected" in (got.error or "")
    assert got.items == ()


def test_github_being_unreachable_is_reported_too() -> None:
    t = FakeTransport(login=LOGIN)
    t.script[f"GET {search_path(LOGIN, None)}"] = [TransportError("connection reset")]
    got = IssueSource(t, LOGIN).fetch(None)
    assert got.ok is False
    assert "could not reach GitHub" in (got.error or "")


def test_an_empty_search_window_does_not_move_the_cursor() -> None:
    """GitHub's search index lags creation. An empty answer is not proof of nothing."""
    t = FakeTransport(login=LOGIN)
    t.script[f"GET {search_path(LOGIN, '2026-09-16T07:00:00Z')}"] = [
        {"total_count": 0, "items": []}
    ]
    got = IssueSource(t, LOGIN).fetch("2026-09-16T07:00:00Z")
    assert got.ok
    assert got.next_cursor is None
    assert IssueSource(t, LOGIN).name == GITHUB_CURSOR


def test_a_partial_search_result_does_not_move_the_cursor_either() -> None:
    """``incomplete_results`` is GitHub timing out its own query, with a 200."""
    t = FakeTransport(login=LOGIN)
    t.script[f"GET {search_path(LOGIN, None)}"] = [
        {
            "total_count": 99,
            "incomplete_results": True,
            "items": [
                {
                    "id": 1,
                    "number": 1,
                    "title": "x",
                    "created_at": "2026-09-17T06:00:00Z",
                    "user": {"login": "ada"},
                    "repository_url": "https://api.github.com/repos/Efkrdnz/a",
                }
            ],
        }
    ]
    got = IssueSource(t, LOGIN).fetch(None)
    assert got.ok
    assert got.items
    assert got.next_cursor is None
    assert got.notes["incomplete"] == "true"


# ───────────────────────── section four: comments ─────────────────────────


def a_thread(cid: str, author: str, text: str, at: str, replies: int = 0) -> dict[str, object]:
    return {
        "id": f"thread-{cid}",
        "snippet": {
            "totalReplyCount": replies,
            "topLevelComment": {
                "id": cid,
                "snippet": {
                    "authorDisplayName": author,
                    "textOriginal": text,
                    "publishedAt": at,
                },
            },
        },
    }


def test_comments_stop_paging_at_the_cursor_instead_of_reading_everything() -> None:
    api = FakeYoutubeApi(
        pages=[
            {
                "items": [
                    a_thread("c3", "ada", "third", "2026-09-17T09:00:00Z", replies=2),
                    a_thread("c2", "bob", "second", "2026-09-16T09:00:00Z"),
                ],
                "nextPageToken": "p2",
            },
            {
                "items": [a_thread("c1", "cat", "first", "2026-09-01T09:00:00Z")],
                "nextPageToken": "p3",
            },
            {"items": [a_thread("c0", "dot", "zeroth", "2026-08-01T09:00:00Z")]},
        ]
    )
    got = CommentSource(api, "UC123").fetch("2026-09-16T09:00:00Z")

    assert got.ok
    # c2 sits exactly ON the cursor and comes back, because every cursor here is
    # inclusive; the seen set is what stops it being said twice. c1 is older, so
    # paging stops there and the third page is never asked for.
    assert [i.id for i in got.items] == ["yt:c3", "yt:c2"]
    assert "(2 replies)" in got.items[0].line
    assert len(api.calls) == 2
    assert got.notes["quota_units"] == "2"
    assert got.next_cursor == "2026-09-17T09:00:00Z"


def test_a_spent_youtube_quota_is_its_own_sentence() -> None:
    api = FakeYoutubeApi(pages=[QuotaExceeded("quotaExceeded")])
    got = CommentSource(api, "UC123").fetch(None)
    assert got.ok is False
    assert "quota is spent" in (got.error or "")


def test_an_unconnected_channel_says_so() -> None:
    got = UnconnectedChannel().fetch(None)
    assert got.ok is False
    assert "not connected" in (got.error or "")
    assert UnconnectedChannel().name == YOUTUBE_CURSOR
