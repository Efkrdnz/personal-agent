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
    a channel (telegram, capture) knows the spine
    the desk (voice, audio) knows the spine
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
    ),
    # A channel reaches Claude Code through rows in ``requests`` and nothing
    # else. Importing the desk would make it unable to run on a headless box,
    # which is every box it will ever run on.
    "jarvis/telegram": ("jarvis.cc", "jarvis.voice", "jarvis.audio", "jarvis.live"),
    "jarvis/capture": ("jarvis.cc", "jarvis.voice", "jarvis.audio", "jarvis.live"),
    # The desk speaks and listens. Which channel is attached is not its business,
    # and the driver is a process it talks to through the database.
    "jarvis/voice": ("jarvis.cc", "jarvis.telegram"),
    "jarvis/audio": ("jarvis.cc", "jarvis.telegram"),
    # The other half of the same seam, and the half the answer-shape move was
    # about: the driver NEVER SPEAKS and does not know which channel is attached.
    # Without this the guard is one-directional — it would have caught the voice
    # layer importing the driver while saying nothing about the driver importing
    # an audio device, and the second is what makes the phone stage a rewrite.
    "jarvis/cc": ("jarvis.voice", "jarvis.audio", "jarvis.live", "jarvis.telegram"),
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
