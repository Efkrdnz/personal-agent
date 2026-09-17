"""Section three: new issues on the user's repositories. ONE search call, on the existing seam.

Reuses :class:`jarvis.github.transport.Transport` — the client stage 4 built,
with its 403-is-two-things handling, its token that never reaches a repr, and
its fake. There is no second GitHub client in this tree and there must not be.

WHY ONE SEARCH CALL AND NOT N PER-REPO CALLS. ``GET /search/issues`` is metered
in a SEPARATE RATE-LIMIT BUCKET from core: 30 requests a minute against search,
while ``GET /repos/{owner}/{repo}/issues`` spends the 5,000-an-hour core budget
that every other part of Jarvis — the repo creation read-back, the capability
matrix, the PR checks — also spends. One search query for every repository the
user owns costs one request from a budget nothing else competes for; the
per-repo loop costs one request per repo from the budget everything else needs,
and it grows with the account. The query is
``user:<login> is:issue created:>=<cursor>``.

``created:>=`` IS INCLUSIVE, which is the same decision
:func:`jarvis.reconcile.set_briefing_cursor` documents: the issue created in the
same second as the cursor comes back tomorrow rather than being dropped, and the
``briefing_seen`` set is what removes the duplicate. Note also that GitHub's
search index lags issue creation by seconds — another reason the cursor must
never jump past a window it merely failed to find anything in.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jarvis.briefing.sources import CandidateItem, Fetch, down
from jarvis.github.transport import (
    GithubError,
    RateLimited,
    Transport,
    TransportError,
    Unauthorized,
)

__all__ = [
    "GITHUB_CURSOR",
    "PER_PAGE",
    "SEARCH_PATH",
    "IssueSource",
    "issue_item",
    "search_path",
]

#: The row in ``cursors`` this source owns; the name 001_init.sql already uses.
GITHUB_CURSOR = "github_issues_since"

SEARCH_PATH = "/search/issues"

#: Thirty is already more issues than anybody wants read aloud before breakfast;
#: the section says the first few and the count. A second page would be a second
#: request against the small bucket for items nobody will hear.
PER_PAGE = 30


def search_path(login: str, cursor: str | None) -> str:
    """The one path this module ever requests, query included and encoded.

    Built here rather than inline so the test that pins "exactly one search call,
    with this query" compares against the same string the code sends.
    """
    q = f"user:{login} is:issue"
    if cursor:
        # Seconds precision: GitHub's search qualifiers do not accept the
        # milliseconds jarvis.ids.now writes, and send one and the whole query is
        # silently ignored rather than rejected.
        q += f" created:>={cursor[:19]}Z" if len(cursor) > 19 else f" created:>={cursor}"
    query = urllib.parse.urlencode(
        {"q": q, "sort": "created", "order": "asc", "per_page": PER_PAGE}
    )
    return f"{SEARCH_PATH}?{query}"


def _repo_of(item: Mapping[str, Any]) -> str:
    """``https://api.github.com/repos/o/n`` -> ``o/n``.

    Search results carry ``repository_url`` and no ``repository`` object, which
    is the one field people expect and do not get.
    """
    url = str(item.get("repository_url") or "")
    _, _, tail = url.partition("/repos/")
    return tail or "a repository"


def issue_item(item: Mapping[str, Any]) -> CandidateItem:
    """One search result as a candidate. Identity is GitHub's numeric issue id."""
    repo = _repo_of(item)
    number = item.get("number")
    title = str(item.get("title") or "").strip() or "(no title)"
    who = str((item.get("user") or {}).get("login") or "someone")
    return CandidateItem(
        id=f"issue:{item.get('id')}",
        line=f"{who} opened {repo} issue {number}: {title}",
        at=str(item.get("created_at")) if item.get("created_at") else None,
        detail=str(item.get("body") or "")[:400],
        url=str(item.get("html_url")) if item.get("html_url") else None,
    )


@dataclass(frozen=True, slots=True)
class IssueSource:
    """New issues across everything ``login`` owns, in one request."""

    transport: Transport
    login: str
    name: str = GITHUB_CURSOR

    def fetch(self, cursor: str | None) -> Fetch:
        path = search_path(self.login, cursor)
        try:
            resp = self.transport.request("GET", path)
        except Unauthorized:
            # The overnight failure this section is most likely to have, and the
            # one where "no new issues" would be the most damaging thing to say.
            return down("my GitHub token was rejected, so I could not check your issues")
        except RateLimited as exc:
            return down(f"GitHub rate-limited the issue search ({exc.resource or 'search'})")
        except GithubError as exc:
            return down(f"GitHub refused the issue search: {exc.message}")
        except TransportError as exc:
            return down(f"I could not reach GitHub to check your issues: {exc}")
        except Exception as exc:
            return down(f"I could not check your issues: {exc}")

        body = resp.body if isinstance(resp.body, Mapping) else {}
        raw = [i for i in body.get("items") or () if isinstance(i, Mapping)]
        items = tuple(issue_item(i) for i in raw)
        notes = {"total": str(body.get("total_count", len(items)))}
        if body.get("incomplete_results"):
            # GitHub timed out its own query and returned a PARTIAL answer with a
            # 200. Recorded, because the cursor must not advance as if this were
            # the whole truth.
            notes["incomplete"] = "true"
            return Fetch(items=items, next_cursor=None, notes=notes)
        stamps = [i.at for i in items if i.at]
        return Fetch(
            items=items,
            # No items means NO ADVANCE, deliberately. The search index lags
            # creation, so an empty window is not proof the window was empty —
            # moving the cursor past it is how an issue is lost forever.
            next_cursor=max(stamps) if stamps else None,
            notes=notes,
        )
