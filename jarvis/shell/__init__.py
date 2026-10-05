"""Terminal commands: what may run, how it is read back, and how it runs.

Two modules, split on purity:

``command``  Pure text. Refuses a command whose read-back could not be true
             (hidden characters, encoded payloads, too long to hear), builds the
             read-back in code, and turns the bytes a console program printed
             into text a model can read.
``run``      The process: the right shell by absolute path, hidden, in the home
             folder, with the secrets taken out of its environment, bounded in
             time and in memory, and killed as a TREE.

WHAT THIS PACKAGE DOES NOT DO is decide whether a command should run. That is
the tool's job (:mod:`jarvis.tools.builtin.computer`) and the user's yes
(:mod:`jarvis.tools.confirm`). Nothing here knows who asked, over which
channel, or how the yes was given, so nothing here can be talked into skipping
it.

Standard library plus the spine's secret table, like :mod:`jarvis.capture`:
it imports under ``python -S``, so every process that may offer the tool can
import it.
"""

from __future__ import annotations

from jarvis.shell.command import (
    ENV_KEY,
    FULL_TEXT_WHERE,
    MAX_CHARS,
    MAX_LINES,
    SHORT_CHARS,
    WRAPPER,
    Refused,
    check,
    clean,
    clip,
    decode,
    readback,
    speakable,
    warnings,
)

# Not ``run`` itself: re-exporting the function here would shadow the
# ``jarvis.shell.run`` submodule, and ``from jarvis.shell import run`` would then
# mean two different things depending on import order.
from jarvis.shell.run import (
    CREATE_NO_WINDOW,
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    Outcome,
    Shell,
    ShellSpec,
    code_pages,
    kill_tree,
    resolve,
    scrubbed_env,
)

__all__ = [
    "CREATE_NO_WINDOW",
    "DEFAULT_TIMEOUT_S",
    "ENV_KEY",
    "FULL_TEXT_WHERE",
    "MAX_CHARS",
    "MAX_LINES",
    "MAX_TIMEOUT_S",
    "SHORT_CHARS",
    "WRAPPER",
    "Outcome",
    "Refused",
    "Shell",
    "ShellSpec",
    "check",
    "clean",
    "clip",
    "code_pages",
    "decode",
    "kill_tree",
    "readback",
    "resolve",
    "scrubbed_env",
    "speakable",
    "warnings",
]
