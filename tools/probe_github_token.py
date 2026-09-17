#!/usr/bin/env python3
"""Spike S8 — what can this GitHub token actually do to a repository?

The whole spoken undo wording for repo creation depends on the answer, and the
roadmap's method was "on a throwaway repo, try it and see". That works, and when
the answer is "no delete" it leaves a repository behind on the user's real
account — the exact harm stage 4 is about.

So this probe is read-only by default and gets most of the matrix for free:
``X-OAuth-Scopes`` comes back on any authenticated request, so ONE ``GET /user``
answers delete / archive / rename / make-private for a classic token, creates
nothing, and leaves nothing behind.

    python tools/probe_github_token.py                     # read-only. Always start here.
    python tools/probe_github_token.py --json out.json     # same, machine-readable

A fine-grained PAT does not report its permissions in a header, so it comes back
``unknown`` for everything, and unknown is the one answer a header cannot improve.
THAT is when the destructive half earns its keep:

    python tools/probe_github_token.py --create-throwaway --owner <login>

It prints, in advance and by name, exactly what will be left behind if delete
turns out to be unavailable, and then asks. Nothing is created before you answer.

The token is read from the OS keyring and from JARVIS_GITHUB_TOKEN, and from
nowhere else — deliberately: GITHUB_TOKEN and GH_TOKEN belong to whatever tool
put them there, and a probe that creates repositories must use the credential it
was GIVEN rather than the first one it can find. It is never printed, never passed
as an argument (an argument is in ``ps`` and in the shell history), and never
interpolated into an error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.github import repos  # noqa: E402 - the repo root is not on sys.path at import time
from jarvis.github.scopes import (  # noqa: E402
    OPERATIONS,
    abandoned_name,
    capabilities,
    implied_reversibility,
    spoken_capability_line,
)
from jarvis.github.transport import (  # noqa: E402
    GithubError,
    HttpTransport,
    RateLimited,
    Transport,
    TransportError,
    Unauthorized,
)

__all__ = [
    "EXIT_BAD_TOKEN",
    "EXIT_CRASHED",
    "EXIT_NO_TOKEN",
    "EXIT_OK",
    "KEYRING_SERVICE",
    "KEYRING_USER",
    "NO_TOKEN_MESSAGE",
    "TOKEN_ENV",
    "build_parser",
    "main",
    "read_only_report",
    "resolve_token",
    "throwaway_warning",
]

EXIT_OK = 0
EXIT_CRASHED = 1
EXIT_NO_TOKEN = 2
EXIT_BAD_TOKEN = 3

TOKEN_ENV = "JARVIS_GITHUB_TOKEN"
KEYRING_SERVICE = "jarvis"
KEYRING_USER = "github_token"

NO_TOKEN_MESSAGE = f"""No GitHub token for Jarvis, so there is nothing to probe.

Put one where Jarvis will look for it, and nowhere else:

    keyring set {KEYRING_SERVICE} {KEYRING_USER}      # preferred: the OS keyring
    export {TOKEN_ENV}=…                    # headless box, this shell only

This probe deliberately ignores GITHUB_TOKEN and GH_TOKEN even when they are set:
they belong to whatever tool put them there, and a probe that can create a
repository should use the credential it was given rather than the first one it
finds. Never pass a token as a command-line argument — that puts it in ps and in
your shell history.
"""


def resolve_token() -> str | None:
    """The token, from the keyring first and one named environment variable second.

    ``keyring`` is imported inside the function and its absence is not an error:
    the house rule is that no secret enters the tree, not that every machine has
    a keyring daemon.
    """
    try:  # noqa: SIM105 - the fallback is the point, not a suppression
        import keyring  # type: ignore[import-not-found]

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if stored:
            return str(stored)
    except Exception:  # noqa: BLE001 - no keyring, no backend, locked keyring
        pass
    return os.environ.get(TOKEN_ENV) or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="probe_github_token.py", description=__doc__)
    p.add_argument("--json", default=None, help="also write the findings to this file")
    p.add_argument(
        "--create-throwaway",
        action="store_true",
        help="the destructive half: create a real repository and try to delete it",
    )
    p.add_argument("--owner", default=None, help="owner for --create-throwaway (default: you)")
    p.add_argument(
        "--slug",
        default="jarvis-s8-probe",
        help="name for the throwaway repository",
    )
    p.add_argument(
        "--assume-yes",
        action="store_true",
        help="skip the typed confirmation for --create-throwaway (it still prints the warning)",
    )
    return p


# ───────────────────────────── the read-only half ─────────────────────────────


def read_only_report(transport: Transport) -> dict[str, Any]:
    """One request, no writes, and most of the matrix."""
    caps = capabilities(transport)
    return {
        "capabilities": caps.as_dict(),
        "implied_reversibility": implied_reversibility(caps),
        "spoken": spoken_capability_line(caps, slug="comment-watcher"),
    }


def print_read_only(report: dict[str, Any]) -> None:
    caps = report["capabilities"]
    print(f"token kind   : {caps['token_kind']}")
    print(f"login        : {caps['login']}")
    print(f"scopes       : {_scopes_text(caps['scopes'])}")
    print(f"read from    : {caps['source']}")
    print()
    for op in OPERATIONS:
        print(f"  {op:<12}: {caps[op]}")
    print()
    for note in caps["notes"]:
        print(f"  note: {note}")
    print()
    print(f"class before acting : {report['implied_reversibility']}")
    print(f"Jarvis would say    : {report['spoken']}")


def _scopes_text(scopes: list[str] | None) -> str:
    if scopes is None:
        return "(no X-OAuth-Scopes header — a fine-grained or app token)"
    return ", ".join(scopes) if scopes else "(header present but empty: no scopes at all)"


# ───────────────────────────── the destructive half ─────────────────────────────


def throwaway_warning(owner: str, slug: str, caps_delete: str) -> str:
    """Exactly what this will leave behind, by name, BEFORE anything is created."""
    full = f"{owner}/{slug}"
    lines = [
        "",
        "--create-throwaway is about to CREATE A REAL REPOSITORY on a real account.",
        "",
        f"  it will create : github.com/{full}  (private, empty, no commits)",
        "  it will then   : try DELETE, and report whether that worked",
        "",
        f"The scope header says delete is {caps_delete!r}.",
        "",
        "IF DELETE DOES NOT WORK, this is what you are left with, and it is permanent:",
        "",
        f"  a repository at github.com/{owner}/{abandoned_name(slug)}",
        "  archived, private, empty, renamed out of the way — and undeletable by this",
        "  token. You will have to delete it by hand in the GitHub UI, or leave it.",
        "",
        "Nothing has been created yet.",
        "",
    ]
    return "\n".join(lines)


def run_throwaway(transport: Transport, owner: str, slug: str) -> dict[str, Any]:
    """Create, try to delete, and compensate in order if delete is refused."""
    out: dict[str, Any] = {"owner": owner, "slug": slug, "steps": []}

    repo = repos.create(transport, owner, slug)
    out["created"] = repo.as_provider_ref()
    out["steps"].append({"op": "create", "ok": True, "full_name": repo.full_name})
    print(f"created {repo.html_url}")

    try:
        transport.request("DELETE", f"/repos/{owner}/{slug}", retry_safe=False)
    except GithubError as e:
        out["delete"] = {"ok": False, "status": e.status, "message": e.message}
        out["steps"].append({"op": "delete", "ok": False, "status": e.status})
        print(f"DELETE refused: {e.status} {e.message}")
    else:
        out["delete"] = {"ok": True}
        out["steps"].append({"op": "delete", "ok": True})
        print("DELETE worked — the repository is gone and nothing was left behind.")
        return out

    # Delete is unavailable, so this is the real compensation, in the order that
    # works: archive LAST, because an archived repository is read-only.
    current = slug
    for op in repos.COMPENSATION_ORDER:
        try:
            if op == "rename":
                new = abandoned_name(slug)
                repos.rename(transport, owner, current, new)
                current = new
            elif op == "set_private":
                repos.set_private(transport, owner, current)
            else:
                repos.archive(transport, owner, current)
        except GithubError as e:
            out["steps"].append({"op": op, "ok": False, "status": e.status, "message": e.message})
            print(f"{op} failed: {e.status} {e.message}")
            continue
        out["steps"].append({"op": op, "ok": True})
        print(f"{op} worked")

    out["left_behind"] = f"{owner}/{current}"
    print(f"\nLEFT BEHIND, permanently: github.com/{owner}/{current}")
    return out


# ───────────────────────────── entry point ─────────────────────────────


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    args = build_parser().parse_args(argv)

    if transport is None:
        token = resolve_token()
        if not token:
            print(NO_TOKEN_MESSAGE, file=sys.stderr)
            return EXIT_NO_TOKEN
        transport = HttpTransport(token)

    try:
        report = read_only_report(transport)
    except Unauthorized as e:
        print(f"the token was rejected: {e.status} {e.message}", file=sys.stderr)
        return EXIT_BAD_TOKEN
    except RateLimited as e:
        print(
            f"rate limited, not a permission problem: wait {e.retry_after_s:.0f}s and re-run",
            file=sys.stderr,
        )
        return EXIT_CRASHED
    except (GithubError, TransportError) as e:
        print(f"could not read the token's capabilities: {e}", file=sys.stderr)
        return EXIT_CRASHED

    print_read_only(report)

    if args.create_throwaway:
        owner = args.owner or report["capabilities"]["login"]
        if not owner:
            print("--create-throwaway needs --owner: nothing named the account", file=sys.stderr)
            return EXIT_CRASHED
        print(throwaway_warning(owner, args.slug, report["capabilities"]["delete"]))
        if not args.assume_yes and not _confirmed():
            print("nothing was created")
            return EXIT_OK
        try:
            report["throwaway"] = run_throwaway(transport, owner, args.slug)
        except (GithubError, TransportError) as e:
            report["throwaway"] = {"error": str(e)}
            print(f"the throwaway run failed: {e}", file=sys.stderr)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return EXIT_OK


def _confirmed() -> bool:
    """A typed word, not a keypress: this one creates something that may be permanent."""
    try:
        answer = input("Type 'create' to go ahead: ").strip().lower()
    except EOFError:
        return False
    return answer == "create"


if __name__ == "__main__":
    sys.exit(main())
