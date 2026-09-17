#!/usr/bin/env python3
"""Fail if a layer imports a layer it is not allowed to know about.

Rule 4 says the spine imports on a bare interpreter. That is a check about
PACKAGES; this is the check about DIRECTION, and it is the one that actually
keeps the seams honest. ``jarvis.answers`` used to live in :mod:`jarvis.cc`, and
because it did, the Telegram channel and the voice layer both imported the Claude
Code driver's package to get at a pure numbering function. Nothing broke, no test
failed, and the two falsifiable seam claims in CLAUDE.md were quietly false.

The direction is inwards, always:

    the spine knows nothing about anybody
    the github client knows the spine
    the project lifecycle knows the spine and the github client
    a channel (telegram, capture) knows the spine and the project lifecycle
    the desk (voice, audio) knows the spine and the project lifecycle
    the driver (cc) knows the spine, and neither a channel nor a sound card
    only the top-level apps know all three

WHY THE AST AND NOT A GREP. Half the modules here NAME other layers in their
docstrings on purpose — that is how a reader finds out where the numbering lives —
so a string search fails on correct prose. And an import inside a function body is
exactly how somebody reaches across a seam "just this once", so a search for
lines starting with ``import`` misses the case worth catching. ``ast.walk`` sees
every real import and no prose at all.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

__all__ = ["RULES", "imports_of", "modules_of", "violations"]

#: Layer -> the module prefixes it must never import. Keys are repo-relative
#: directories, except "spine", which is the top-level ``jarvis/*.py`` modules.
RULES: dict[str, tuple[str, ...]] = {
    # The spine is the bottom. Every process opens the same SQLite file,
    # including ones that will never have a sound card, a Telegram token or the
    # Claude Code CLI installed.
    "spine": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.capture",
        "jarvis.github",
        # Stage 4's upper layer. `jarvis.effects` classifying `github.repo_create`
        # is NOT the spine knowing how to make one: the spine has to keep
        # importing under `python -S` on a box with no token and no workspace.
        "jarvis.project",
        # Stage 5's. `jarvis.reconcile.project_status` composes the briefing's
        # first section and `jarvis.requests` carries every other one, but
        # neither may know the briefing exists: the briefing reads Gmail, GitHub
        # and YouTube, and the spine has to keep importing on a box with no
        # network at all.
        "jarvis.briefing",
        # The other half of stage 5. `requests` knows the `briefing_gate` KIND —
        # what such a question looks like — and must never know WHEN it gets
        # asked: the thing that wakes up at ten reads presence, writes deliveries
        # and has a timezone database behind it, none of which may be on the
        # import path of a runner with no schedule at all.
        "jarvis.schedule",
    ),
    # A channel reaches Claude Code through rows in ``requests`` and nothing
    # else. Importing the desk would make it unable to run on a headless box,
    # which is every box it will ever run on.
    "jarvis/telegram": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.github",
        # And it does not decide WHEN it is asked anything. The scheduler
        # writes the delivery rows this channel picks up; a channel that
        # could import it could brief the user on itself.
        "jarvis.schedule",
    ),
    "jarvis/capture": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.github",
        "jarvis.schedule",
    ),
    # An outward PROVIDER adapter: it reaches GitHub over HTTPS and knows the
    # spine's vocabulary (jarvis.effects owns the promise words its spoken line is
    # checked against) and nothing else. In particular not a channel: whether the
    # confirmation was tapped on Telegram or spoken at the desk is none of its
    # business, and the whole capability matrix has to be readable on a headless
    # box with no sound card. The reverse direction is the one above: the spine
    # must keep importing under `python -S`, and urllib.request is not that.
    "jarvis/github": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.capture",
        # The provider adapter is BELOW the lifecycle that drives it. It knows how
        # to create a repository; whether one should be created, what it is called
        # and which job it belongs to are decisions it must never be able to read.
        "jarvis.project",
        # Same direction, same reason: the briefing reads issues THROUGH this
        # client. A client that could import the briefing would be able to decide
        # what is worth saying at ten in the morning.
        "jarvis.briefing",
        "jarvis.schedule",
    ),
    # THE PROJECT LIFECYCLE: above the github client, below everything that
    # speaks. It raises `requests` rows, writes `effects` and `outbox` rows and
    # creates the `jobs` row with a cwd — and that row is the ENTIRE handover to
    # the driver, which is why reaching into `jarvis.cc` from here would quietly
    # make stage 6's "phone touches zero files in jarvis/cc" claim false. It must
    # also not know which channel will present its read-back: the same
    # confirmation is tapped on Telegram, spoken at the desk and keyed on a phone,
    # and a lifecycle that imported one of them could only ever be answered there.
    "jarvis/project": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.capture",
        "jarvis.schedule",
    ),
    # A BRIEFING DOES NOT KNOW HOW IT IS DELIVERED. That is the whole point of
    # the stage: a section is a row in `requests`, and which channel says it is
    # decided by presence and the router, hours later, in another process. An
    # import of the desk here would mean the briefing could only ever be spoken;
    # an import of Telegram, only ever tapped; and stage 6's phone would be a
    # rewrite rather than one more channel reading the same rows. It may reach
    # DOWN to the spine and to the GitHub client, and nowhere else.
    "jarvis/briefing": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.capture",
    ),
    # THE SCHEDULER NEVER SPEAKS AND NEVER DIALS. It knows the spine, it asks
    # presence where the user is, and it writes `deliveries` rows naming a
    # channel as a STRING. That is the whole of stage 6's "the phone is one more
    # channel" claim: an import of Telegram here would mean the morning briefing
    # could only ever be tapped, and an import of the desk would mean it could
    # only ever be spoken. It must not know what a briefing CONTAINS either —
    # the gate publishes `briefing.started` and stops — so `jarvis.briefing` is
    # on the list, and the dependency between the two stage-5 halves points that
    # way round: the briefing may read the schedule, never the reverse.
    "jarvis/schedule": (
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.capture",
        "jarvis.github",
        "jarvis.project",
        "jarvis.briefing",
    ),
    # The desk speaks and listens. Which channel is attached is not its business,
    # and the driver is a process it talks to through the database.
    "jarvis/voice": ("jarvis.cc", "jarvis.telegram", "jarvis.github", "jarvis.schedule"),
    "jarvis/audio": ("jarvis.cc", "jarvis.telegram", "jarvis.github", "jarvis.schedule"),
    # The other half of the same seam, and the half the answer-shape move was
    # about: the driver NEVER SPEAKS and does not know which channel is attached.
    # Without this the guard is one-directional — it would have caught the voice
    # layer importing the driver while saying nothing about the driver importing
    # an audio device, and the second is what makes the phone stage a rewrite.
    # `jarvis.briefing` joins that list for the same reason: the driver is asked
    # questions, it never composes them, and a driver that imported the briefing
    # would be a driver that needs a GitHub client to start.
    "jarvis/cc": (
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.briefing",
        "jarvis.schedule",
    ),
}

#: The one key in RULES that is not a directory. Named so the key and the lookup
#: cannot drift, which would silently turn the spine's rule into an empty glob.
SPINE = "spine"


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def modules_of(layer: str, root: Path | None = None) -> list[Path]:
    """The source files belonging to one layer, sorted."""
    base = (root or _root()).resolve()
    if layer == SPINE:
        return sorted((base / "jarvis").glob("*.py"))
    return sorted((base / layer).rglob("*.py"))


def imports_of(path: Path, root: Path | None = None) -> set[str]:
    """Every module this file really imports, as absolute dotted names.

    Relative imports are resolved against the file's own package, because
    ``from .. import cc`` is the same reach across the seam as naming it outright.
    """
    base = (root or _root()).resolve()
    parts = list(path.resolve().relative_to(base).with_suffix("").parts)
    package = parts[:-1]  # jarvis/voice/chunk.py and jarvis/voice/__init__.py both -> jarvis.voice

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            anchor = package[: len(package) - (node.level - 1)] if node.level else []
            prefix = [*anchor, node.module] if node.module else list(anchor)
            if prefix:
                found.add(".".join(prefix))
            # `from x import y` may name a SUBMODULE rather than an attribute, and
            # from here the two are indistinguishable, so both readings are checked.
            found.update(".".join([*prefix, alias.name]) for alias in node.names)
    return found


def _forbidden(name: str, prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        # On a dotted boundary only: a future jarvis.ccache is not jarvis.cc.
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    return None


def violations(root: Path | None = None) -> list[str]:
    """One line per backwards import, empty when the layering holds."""
    base = (root or _root()).resolve()
    out: list[str] = []
    for layer, prefixes in RULES.items():
        files = modules_of(layer, base)
        if not files:
            out.append(f"{layer}: no modules found; this check would pass vacuously")
            continue
        for path in files:
            for name in sorted(imports_of(path, base)):
                if hit := _forbidden(name, prefixes):
                    rel = path.resolve().relative_to(base).as_posix()
                    out.append(f"{rel}: imports {name} — {layer} may not depend on {hit}")
    return out


def main() -> int:
    bad = violations()
    for line in bad:
        print(line, file=sys.stderr)
    if bad:
        print(
            "\nDependencies point inwards. If a layer needs something from another, the\n"
            "thing it needs probably belongs in the spine — move it there rather than\n"
            "importing across the seam. See CLAUDE.md, 'Two falsifiable tests'.",
            file=sys.stderr,
        )
        return 1
    print(f"ok: {len(RULES)} layers, no backwards imports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
