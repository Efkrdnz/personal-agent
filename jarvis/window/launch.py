"""Open the window as an app window, falling back to an ordinary browser tab.

WHY ``--app=``. Edge and Chrome open a URL with ``--app`` as a window of its own:
no address bar, no tabs, its own taskbar entry. That is the difference between
"a HUD" and "a page I lose among forty tabs", and it costs no GUI toolkit.

WHY NOT ``shutil.which`` ALONE. On Windows, Edge is installed on every machine
and is almost never on PATH; Chrome usually is not either. So the well-known
install directories are probed too, built from the environment rather than
hard-coded drive letters, because ``Program Files`` is not always on ``C:``.

THE DECISION IS PURE. :func:`candidates` and :func:`app_command` take the
lookups (``which``, ``exists``, ``env``, ``platform``) as arguments and return
data, so every branch is testable on any OS. :func:`open_window` is the one
function with a side effect.

NEVER A SHELL. The URL carries the window's token and is data, while a shell
reads ``&``, ``#`` and ``|`` as syntax (``cmd`` splits a command line at the
first ``&``). The argv is a list handed straight to the process.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping
from typing import Any, Literal

__all__ = [
    "WINDOW_SIZE",
    "Opened",
    "app_argv",
    "app_command",
    "candidates",
    "open_window",
]

Opened = Literal["edge-app", "chrome-app", "browser"]

#: Big enough for three columns at the layout's comfortable width.
WINDOW_SIZE = "1440,900"

Which = Callable[[str], str | None]
Exists = Callable[[str], bool]

# On PATH, in preference order. Edge first: it ships with Windows, so it is the
# one most likely to be there, and either one gives the same app window.
_EDGE_NAMES = ("msedge", "microsoft-edge", "microsoft-edge-stable")
_CHROME_NAMES = ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

# Windows: install roots from the environment, and the path under each.
_WIN_EDGE = (
    ("ProgramFiles(x86)", ("Microsoft", "Edge", "Application", "msedge.exe")),
    ("ProgramFiles", ("Microsoft", "Edge", "Application", "msedge.exe")),
    ("LOCALAPPDATA", ("Microsoft", "Edge", "Application", "msedge.exe")),
)
_WIN_CHROME = (
    ("ProgramFiles", ("Google", "Chrome", "Application", "chrome.exe")),
    ("ProgramFiles(x86)", ("Google", "Chrome", "Application", "chrome.exe")),
    ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
)

# macOS: an .app bundle's binary is never on PATH.
_MAC_EDGE = ("Microsoft Edge.app", "Contents", "MacOS", "Microsoft Edge")
_MAC_CHROME = ("Google Chrome.app", "Contents", "MacOS", "Google Chrome")
_MAC_CHROMIUM = ("Chromium.app", "Contents", "MacOS", "Chromium")


def candidates(
    *,
    which: Which = shutil.which,
    exists: Exists = os.path.exists,
    env: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
) -> list[tuple[Opened, str]]:
    """Every installed browser that can open ``--app``, best first, without repeats."""
    edge: list[str] = [p for p in (which(n) for n in _EDGE_NAMES) if p]
    chrome: list[str] = [p for p in (which(n) for n in _CHROME_NAMES) if p]
    if platform.startswith("win"):
        # ntpath, not os.path: the decision is the same whatever OS runs the
        # test, and a Windows path joined with "/" is a path nobody has.
        edge += _win_paths(_WIN_EDGE, env, exists)
        chrome += _win_paths(_WIN_CHROME, env, exists)
    elif platform == "darwin":
        roots = ["/Applications"]
        home = env.get("HOME")
        if home:
            roots.append(f"{home.rstrip('/')}/Applications")
        edge += _mac_paths(roots, (_MAC_EDGE,), exists)
        chrome += _mac_paths(roots, (_MAC_CHROME, _MAC_CHROMIUM), exists)
    out: list[tuple[Opened, str]] = []
    seen: set[str] = set()
    groups: tuple[tuple[Opened, list[str]], ...] = (("edge-app", edge), ("chrome-app", chrome))
    for how, paths in groups:
        for path in paths:
            # Windows paths are case-insensitive: which() and the probe can name
            # one msedge.exe two ways, and trying it twice is a wasted launch.
            folded = path.lower() if platform.startswith("win") else path
            if folded not in seen:
                seen.add(folded)
                out.append((how, path))
    return out


def app_argv(exe: str, url: str) -> list[str]:
    """The command line that opens ``url`` as an app window. One argv element per flag."""
    return [exe, f"--app={url}", f"--window-size={WINDOW_SIZE}"]


def app_command(
    url: str,
    *,
    which: Which = shutil.which,
    exists: Exists = os.path.exists,
    env: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
) -> tuple[Opened, list[str]] | None:
    """What :func:`open_window` would try first, or None when only a browser tab is left."""
    found = candidates(which=which, exists=exists, env=env, platform=platform)
    if not found:
        return None
    how, exe = found[0]
    return how, app_argv(exe, url)


def open_window(
    url: str,
    *,
    which: Which = shutil.which,
    exists: Exists = os.path.exists,
    run: Callable[..., Any] = subprocess.Popen,
    browser: Callable[[str], Any] = webbrowser.open,
    env: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
) -> Opened:
    """Open ``url`` as an app window if any browser can, else as a tab. Says which.

    A candidate that fails to START (a stale PATH entry, a half-uninstalled
    Edge) falls through to the next one rather than ending the attempt: the
    user asked for a window, and a second browser is still a window.
    """
    for how, exe in candidates(which=which, exists=exists, env=env, platform=platform):
        try:
            run(app_argv(exe, url), **_detached(platform))
        except OSError:
            continue
        return how
    browser(url)
    return "browser"


def _detached(platform: str) -> dict[str, Any]:
    """Popen options that let the browser outlive this process and its terminal."""
    opts: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if platform.startswith("win"):
        # Not attached to this console, and not in its Ctrl+C group: closing
        # the terminal running the window must not close the user's browser.
        opts["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        # The same on POSIX: if this launch IS the browser's first process, a
        # Ctrl+C sent to our process group would otherwise kill every tab.
        opts["start_new_session"] = True
    return opts


def _win_paths(
    table: tuple[tuple[str, tuple[str, ...]], ...], env: Mapping[str, str], exists: Exists
) -> list[str]:
    # Windows variable names are case-insensitive. os.environ honours that, a
    # plain dict copy of it (keys upper-cased) does not, and the caller may
    # hand either.
    folded = {str(k).upper(): v for k, v in env.items()}
    out: list[str] = []
    for var, tail in table:
        root = folded.get(var.upper())
        if root:
            path = ntpath.join(root, *tail)
            if exists(path):
                out.append(path)
    return out


def _mac_paths(roots: list[str], bundles: tuple[tuple[str, ...], ...], exists: Exists) -> list[str]:
    # Bundle-major: Chrome in either Applications folder beats Chromium in both.
    paths = ("/".join((root, *bundle)) for bundle in bundles for root in roots)
    return [p for p in paths if exists(p)]
