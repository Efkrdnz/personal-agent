#!/usr/bin/env python3
"""Fail if any secret pattern in .gitignore is inert.

ADR 0001. The reference build this project learned from carries a .gitignore
whose five secret patterns all match nothing, because each one has a trailing
same-line comment:

    config/api_keys.json          # your Gemini API key

.gitignore has no trailing-comment syntax — '#' only opens a comment at the
START of a line — so the whole line, spaces and all, is one literal pattern.
`git add -A` in that tree publishes the key.

This asserts, against real `git check-ignore`, that every path we consider
secret is genuinely ignored, and that things we must commit are genuinely not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MUST_IGNORE = [
    ".env",
    ".env.local",
    "secrets.json",
    "config/api_keys.json",
    "my-credentials.json",
    "a/token.json",
    "a/client_secret_x.json",
    "server.pem",
    "server.key",
    "certs/tls.crt",
    "jarvis.db",
    "jarvis.db-wal",
    "jarvis.db-shm",
    "var/x.log",
    "logs/a.log",
    "screenshots/s.png",
    "models/w.onnx",
    "__pycache__/x.pyc",
    ".venv/bin/python",
]
MUST_NOT_IGNORE = [".env.example", "README.md", "jarvis/db.py", "tests/test_foundation.py"]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    failures: list[str] = []

    for rel in MUST_IGNORE:
        # check-ignore works on paths, not on existing files, but a path under a
        # directory that does not exist still resolves — so use a temp name that
        # cannot collide with real content.
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root)
        if r.returncode != 0:
            failures.append(f"INERT: {rel} is NOT ignored — a secret pattern matches nothing")

    for rel in MUST_NOT_IGNORE:
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root)
        if r.returncode == 0:
            failures.append(f"OVER-MATCHED: {rel} is ignored but must be committed")

    # And the mechanical cause, caught directly: a pattern with a trailing comment.
    gi = (root / ".gitignore").read_text().splitlines()
    for n, line in enumerate(gi, 1):
        s = line.strip()
        if s and not s.startswith("#") and "#" in s:
            failures.append(
                f".gitignore:{n}: trailing comment makes this pattern literal: {line!r}"
            )

    for f in failures:
        print(f, file=sys.stderr)
    if failures:
        print(
            f"\n{len(failures)} problem(s). See docs/adr/0001-clean-room-not-fork.md.",
            file=sys.stderr,
        )
        return 1
    print(f"ok: {len(MUST_IGNORE)} secret patterns fire, {len(MUST_NOT_IGNORE)} tracked paths safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
