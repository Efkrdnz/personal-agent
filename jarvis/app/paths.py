"""Where the app keeps its own files: logs, the lock, and the address of the running copy.

NOT THE DATABASE. ``jarvis.db.default_path()`` stays the one source for that,
because every process — the desk, the scheduler, a runner started from a
terminal — has to find the same file without asking this layer.

WHY LOGS AT ALL. A windowed app has no console: a refusal printed to stderr is
printed to nothing. Every child's output goes to a file here, and the HUD shows
the path, so "why won't it start" always has an answer somebody can open.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

__all__ = ["ROTATE_BYTES", "app_dir", "log_dir", "log_path", "state_file"]

#: A log past this is moved aside when a new run opens it. One generation is
#: kept: the run before this one is what explains a crash; the run before
#: that is noise nobody reads.
ROTATE_BYTES = 5 * 1024 * 1024

# Process names become file names. Anything else is refused rather than
# sanitised, because a name with a separator in it is a bug upstream.
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def app_dir(*, env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """``%LOCALAPPDATA%\\Jarvis`` on Windows; ``$XDG_STATE_HOME/jarvis`` elsewhere.

    LOCALAPPDATA rather than APPDATA: logs and a lock are per machine, and the
    roaming profile would copy them to every PC the user signs in to.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Jarvis"
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "jarvis"


def log_dir() -> Path:
    """``app_dir()/logs``, created on demand."""
    d = app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path(process: str) -> Path:
    """The log for one process, moved to ``.1`` first when it is over the limit.

    Call it when a run is about to open the file: that is the only moment a
    rotation cannot split one run's output across two files.
    """
    if not _NAME.fullmatch(process):
        raise ValueError(f"not a process name: {process!r}")
    path = log_dir() / f"{process}.log"
    with suppress(OSError):
        if path.stat().st_size > ROTATE_BYTES:
            # OSError suppressed on the replace too: on Windows a file another
            # process still has open cannot be renamed, and a log one run too
            # long is better than an app that will not start.
            os.replace(path, path.with_name(f"{process}.log.1"))
    return path


def state_file() -> Path:
    """``app_dir()/app.json``: the pid, port and token of the copy that is running."""
    return app_dir() / "app.json"
