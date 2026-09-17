"""Section four: new comments on the user's channel. Same seam, same fake, no credential.

``commentThreads.list`` with ``allThreadsRelatedToChannelId`` costs **1 quota
unit** against a daily allowance of 10,000, so polling this is effectively free
— there is no reason to be clever about how often the briefing asks, and no
reason to cache. The expensive mistake in this API is the opposite one: paging
through everything every morning. Threads come back ``order=time``, newest
first, so the loop stops at the first item that is not newer than the cursor.

THIS SECTION DELIBERATELY DOES NOT READ REPLIES. ``commentThreads`` returns the
top-level comment plus ``totalReplyCount`` and at most a few preview replies;
the actual replies need a SEPARATE ``comments.list?parentId=<id>`` call per
thread, which turns one free request into one per thread with a comment. The
briefing says "and three replies" from the count it already has. If somebody
later wants the reply text read out, that is a second source with its own
quota budget, not a loop added here.

As with Gmail there is no credential, so :class:`YoutubeApi` is a protocol with
a deterministic fake and nothing in this module can reach Google.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis.briefing.sources import CandidateItem, Fetch, down, unconfigured

__all__ = [
    "MAX_PAGES",
    "NOT_CONNECTED",
    "PAGE_SIZE",
    "QUOTA_UNITS_PER_CALL",
    "YOUTUBE_CURSOR",
    "CommentSource",
    "FakeYoutubeApi",
    "QuotaExceeded",
    "UnconnectedChannel",
    "YoutubeApi",
    "YoutubeError",
    "thread_item",
]

#: The row in ``cursors`` this source owns; the name 001_init.sql already uses.
YOUTUBE_CURSOR = "youtube_page"

#: Said out loud, verbatim. A sentence, not a noun and a format string.
NOT_CONNECTED = "Your YouTube channel is not connected yet."

PAGE_SIZE = 50

#: The measured cost of one ``commentThreads.list`` call, against 10,000 a day.
#: Written down because it is the fact that decides this section's whole design,
#: and because the next person will otherwise assume it is expensive.
QUOTA_UNITS_PER_CALL = 1

#: A hard stop on paging, so a cursor that has gone missing cannot spend the day's
#: quota in one morning. Ten pages is 500 comments; past that the honest thing is
#: to say "a lot" and move the cursor to the newest one.
MAX_PAGES = 10


class YoutubeError(RuntimeError):
    """The API answered with an error."""


class QuotaExceeded(YoutubeError):
    """403 ``quotaExceeded``. Its own type because the answer is "tomorrow", not "retry".

    At one unit per call this should never happen from the briefing alone, which
    is exactly why it must be reported as itself rather than as a generic
    failure: if the briefing ever sees this, something ELSE is spending the
    quota and that is the interesting fact.
    """


class YoutubeApi(Protocol):
    """One call. See the module docstring for why replies are not a second one."""

    def comment_threads(self, channel_id: str, page_token: str | None = None) -> Mapping[str, Any]:
        """``commentThreads.list(allThreadsRelatedToChannelId=..., order=time)``."""


def _top(thread: Mapping[str, Any]) -> Mapping[str, Any]:
    snippet = thread.get("snippet") or {}
    top = snippet.get("topLevelComment") or {}
    return top.get("snippet") or {}


def thread_item(thread: Mapping[str, Any]) -> CandidateItem:
    """One comment thread as a candidate, with the reply COUNT and not the replies."""
    snippet = thread.get("snippet") or {}
    top = _top(thread)
    top_id = (snippet.get("topLevelComment") or {}).get("id") or thread.get("id")
    author = str(top.get("authorDisplayName") or "someone")
    text = " ".join(str(top.get("textOriginal") or "").split())
    replies = int(snippet.get("totalReplyCount") or 0)
    tail = ""
    if replies == 1:
        tail = " (1 reply)"
    elif replies > 1:
        tail = f" ({replies} replies)"
    return CandidateItem(
        # The top-level comment's id, not the thread's: it is what a later
        # comments.list?parentId= call would use, and it is what stays stable if
        # the thread grows.
        id=f"yt:{top_id}",
        line=f"{author} commented: {text}{tail}",
        at=str(top.get("publishedAt")) if top.get("publishedAt") else None,
        detail=str(top.get("textOriginal") or ""),
    )


@dataclass(frozen=True, slots=True)
class CommentSource:
    """Newest-first paging that stops at the cursor rather than reading everything."""

    api: YoutubeApi
    channel_id: str
    name: str = YOUTUBE_CURSOR

    def fetch(self, cursor: str | None) -> Fetch:
        items: list[CandidateItem] = []
        token: str | None = None
        pages = 0
        try:
            while pages < MAX_PAGES:
                page = self.api.comment_threads(self.channel_id, token)
                pages += 1
                threads = [t for t in page.get("items") or () if isinstance(t, Mapping)]
                stop = False
                for thread in threads:
                    item = thread_item(thread)
                    # Inclusive, like every other cursor here: the comment posted
                    # in the same second keeps coming back and the seen set is
                    # what stops it being said twice.
                    if cursor is not None and item.at is not None and item.at < cursor:
                        stop = True
                        break
                    items.append(item)
                token = str(page.get("nextPageToken")) if page.get("nextPageToken") else None
                if stop or token is None:
                    break
        except QuotaExceeded as exc:
            return down(f"YouTube's quota is spent, so I could not check your comments: {exc}")
        except YoutubeError as exc:
            return down(f"YouTube refused to list your comments: {exc}")
        except Exception as exc:
            return down(f"I could not check your YouTube comments: {exc}")

        stamps = [i.at for i in items if i.at]
        return Fetch(
            items=tuple(items),
            # Same rule as the GitHub search: an empty answer advances nothing.
            next_cursor=max(stamps) if stamps else None,
            notes={"pages": str(pages), "quota_units": str(pages * QUOTA_UNITS_PER_CALL)},
        )


@dataclass(frozen=True, slots=True)
class UnconnectedChannel:
    """No YouTube credential either. Says so instead of saying nothing."""

    name: str = YOUTUBE_CURSOR

    def fetch(self, cursor: str | None) -> Fetch:
        return unconfigured(NOT_CONNECTED)


@dataclass(slots=True)
class FakeYoutubeApi:
    """Scripted pages, in order. A queued exception is raised instead of returned."""

    pages: list[Any] = field(default_factory=list)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def comment_threads(self, channel_id: str, page_token: str | None = None) -> Mapping[str, Any]:
        self.calls.append((channel_id, page_token))
        if not self.pages:
            return {"items": []}
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page
