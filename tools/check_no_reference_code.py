#!/usr/bin/env python3
"""Fail if code from the CC BY-NC reference build appears in this tree.

ADR 0001, rule 1. This repo is MIT and clean-room. Copying even 200 lines of
Mark-LIII would make the entire tree non-commercial forever, and that decision
must be made deliberately and in writing — never accidentally by a paste.

This is a smoke alarm, not a proof: it looks for distinctive identifiers from
the reference implementation. A determined paste with renamed symbols would slip
past, which is why the real control is the rule, not the script.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Identifiers distinctive to the reference build. Generic names (speak, run,
# TOOL) are deliberately excluded — they would fire on our own code.
FINGERPRINTS = [
    "JarvisLive",
    "_ReconnectSignal",
    "_is_reconnect_signal",
    "_keep_context_of",
    "ProactiveEngine",
    "TTSPlayer",
    "_compress_silence",
    "write_log",
    "discover_actions",
    "get_tool_declarations",
    "_CTX_KEYS",
    "ActionRecord",
    "mark-liii",
    "Mark-LIII",
    "FatihMakes",
]
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
# Files where naming the reference is correct: the ADR, credits, the README.
ALLOWED = {
    "docs/adr/0001-clean-room-not-fork.md",
    "CREDITS.md",
    "README.md",
    "CONTRIBUTING.md",
    "docs/findings.md",
    "docs/architecture.md",
    "docs/roadmap.md",
    "tools/check_no_reference_code.py",
}


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    pattern = re.compile("|".join(re.escape(f) for f in FINGERPRINTS))
    hits: list[str] = []

    for p in root.rglob("*"):
        if not p.is_file() or any(d in SKIP_DIRS for d in p.parts):
            continue
        rel = p.relative_to(root).as_posix()
        if rel in ALLOWED or p.suffix not in {".py", ".sql", ".toml", ".cfg"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if m := pattern.search(line):
                hits.append(f"{rel}:{n}: reference fingerprint {m.group()!r}")

    for h in hits:
        print(h, file=sys.stderr)
    if hits:
        print(
            "\nThis tree is MIT and clean-room. See docs/adr/0001-clean-room-not-fork.md:\n"
            "if you genuinely intend to take the CC BY-NC dependency, do it deliberately —\n"
            "add LICENSE, NOTICE and CHANGES.md in the same commit.",
            file=sys.stderr,
        )
        return 1
    print("ok: no reference fingerprints in source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
