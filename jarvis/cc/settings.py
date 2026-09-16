"""The startup assertion: nothing may auto-close a pending question.

``askUserQuestionTimeout`` is a real Claude Code setting. Read out of the shipped
CLI (v2.1.273) its accepted values are exactly::

    "60s" | "5m" | "10m" | "never"

and its own description is "Idle time before Claude's questions auto-continue".
Unset means it blocks indefinitely, and BLOCKING INDEFINITELY IS THE ONLY
CONFIGURATION THIS DESIGN SUPPORTS. The whole product is a question that waits
while the user is on a bus; a managed 60s value would auto-continue the very wait
everything rests on, and the symptom at the desk would be "Jarvis lost my answer"
— an invisible, intermittent, unattributable failure.

So the runner refuses to start and says which file did it. Refusing is cheap:
one process does not start and the user hears why. The alternative is a build
that quietly decides things on the user's behalf.

AN UNREADABLE SETTINGS FILE IS ALSO A REFUSAL. "I could not check" and "it is
fine" are different answers, and only one of them may be assumed by a system
whose entire correctness argument is that the wait is unbounded.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "AUTO_CONTINUE_VALUES",
    "BLOCKS_INDEFINITELY",
    "TIMEOUT_KEY",
    "AskUserQuestionTimeoutSet",
    "SettingsRefusal",
    "UnreadableSettings",
    "assert_no_ask_user_question_timeout",
    "settings_files",
]

TIMEOUT_KEY = "askUserQuestionTimeout"

#: The only value that means what the default means.
BLOCKS_INDEFINITELY: frozenset[str] = frozenset({"never"})

#: Every value the CLI accepts that would reap a pending question.
AUTO_CONTINUE_VALUES: frozenset[str] = frozenset({"60s", "5m", "10m"})


class SettingsRefusal(RuntimeError):
    """The runner will not start. Both subclasses are spoken aloud verbatim."""


class AskUserQuestionTimeoutSet(SettingsRefusal):
    """A timeout that would auto-close a pending question is configured."""

    def __init__(self, path: Path, value: object) -> None:
        self.path, self.value = path, value
        super().__init__(
            f"{path} sets {TIMEOUT_KEY}={value!r}. That auto-continues a question the user "
            f"has not answered yet, which is exactly the wait this system is built on. "
            f"Remove it, or set it to 'never'. Jarvis will not start with it."
        )


class UnreadableSettings(SettingsRefusal):
    """A settings file exists but could not be parsed, so nothing can be proven."""

    def __init__(self, path: Path, why: str) -> None:
        self.path, self.why = path, why
        super().__init__(
            f"{path} could not be read ({why}), so it is impossible to prove that "
            f"{TIMEOUT_KEY} is unset. Refusing to start on an unverified setting."
        )


def _managed_dir(platform: str) -> Path:
    """Where the CLI looks for managed settings, per platform.

    Read out of the shipped binary rather than guessed: ``/etc/claude-code`` on
    Linux, ``/Library/Application Support/ClaudeCode`` on macOS,
    ``C:\\Program Files\\ClaudeCode`` on Windows.
    """
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if platform.startswith("win"):
        return Path("C:/Program Files/ClaudeCode")
    return Path("/etc/claude-code")


def settings_files(
    cwd: str | Path | None = None,
    *,
    platform: str | None = None,
    home: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    """Every file the merged settings could come from, most authoritative first.

    Managed settings come first because they are the ones the runner cannot
    override and therefore the ones most likely to be the cause.
    """
    environ = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    root = Path(home) if home is not None else Path.home()
    config_dir = Path(environ.get("CLAUDE_CONFIG_DIR") or (root / ".claude"))
    project = Path(cwd) if cwd is not None else Path.cwd()
    return (
        _managed_dir(plat) / "managed-settings.json",
        config_dir / "settings.json",
        project / ".claude" / "settings.json",
        project / ".claude" / "settings.local.json",
    )


def assert_no_ask_user_question_timeout(
    paths: Sequence[str | Path] | None = None,
    *,
    cwd: str | Path | None = None,
) -> None:
    """Refuse to start if any settings file would auto-close a question.

    A missing file is fine — that is the default, and the default blocks. A file
    that exists and cannot be parsed is NOT fine; see the module docstring.
    """
    candidates = settings_files(cwd) if paths is None else tuple(Path(p) for p in paths)
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as e:
            raise UnreadableSettings(path, e.strerror or str(e)) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise UnreadableSettings(path, f"invalid JSON at line {e.lineno}") from e
        if not isinstance(data, dict):
            raise UnreadableSettings(path, "top level is not a JSON object")
        if TIMEOUT_KEY not in data:
            continue
        value = data[TIMEOUT_KEY]
        # Anything that is not the literal "never" is refused, INCLUDING a value
        # the CLI does not recognise: an unrecognised value falls back to the
        # CLI's own default handling, and "probably fine" is not a proof.
        if not (isinstance(value, str) and value in BLOCKS_INDEFINITELY):
            raise AskUserQuestionTimeoutSet(path, value)
