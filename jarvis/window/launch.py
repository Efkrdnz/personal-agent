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

NEVER THE TOKEN ON A COMMAND LINE, EITHER. A browser's argv is readable by
every local user through ``/proc/<pid>/cmdline`` on Linux (and ``ps`` on macOS),
and a freshly launched Chrome keeps ``--app=URL`` in its main process for the
whole session. With the token, another account could answer a pending Claude
Code permission as this user — a review reproduced exactly that. So the URL is
written to a private file (0600 in a 0700 directory, or the per-user TEMP on
Windows) that redirects to it, and only that file's PATH goes on the command
line. Jupyter's ``use_redirect_file`` exists for the same reason.
"""

from __future__ import annotations

import html
import ntpath
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "WINDOW_SIZE",
    "Opened",
    "app_argv",
    "app_command",
    "candidates",
    "open_window",
    "redirect_file",
]

#: How long the redirect file outlives the launch. The browser reads it within
#: a second; the margin is for a cold Edge start on a slow disk.
REDIRECT_KEEP_S = 120.0
_REDIRECT_PREFIX = "jarvis-open-"

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
    redirect: Callable[[str], str] | None = None,
) -> Opened:
    """Open ``url`` as an app window if any browser can, else as a tab. Says which.

    A candidate that fails to START (a stale PATH entry, a half-uninstalled
    Edge) falls through to the next one rather than ending the attempt: the
    user asked for a window, and a second browser is still a window.

    What the browser is handed is ``redirect(url)`` — by default a private
    file that redirects to ``url`` — never ``url`` itself. See the module
    docstring for why.
    """
    target = (redirect or redirect_file)(url)
    for how, exe in candidates(which=which, exists=exists, env=env, platform=platform):
        try:
            run(app_argv(exe, target), **_detached(platform))
        except OSError:
            continue
        return how
    browser(target)
    return "browser"


def redirect_file(url: str, *, keep_s: float = REDIRECT_KEEP_S) -> str:
    """A ``file://`` URL whose page sends the browser on to ``url``. Private to this user.

    ``mkdtemp`` makes a 0700 directory (on Windows, inside the per-user TEMP),
    the file is created 0600 with O_EXCL, and a timer removes both once the
    browser has had time to read it. Leftovers from a process that exited
    before its timer fired are swept on the next launch.
    """
    _sweep_old(Path(tempfile.gettempdir()), older_than_s=max(keep_s, 600.0))
    folder = Path(tempfile.mkdtemp(prefix=_REDIRECT_PREFIX))
    page = folder / "open.html"
    safe = html.escape(url, quote=True)
    body = (
        '<!doctype html><meta charset="utf-8"><title>Jarvis</title>'
        f'<meta http-equiv="refresh" content="0;url={safe}">'
        f'<p><a href="{safe}">Open Jarvis</a></p>'
    )
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    timer = threading.Timer(keep_s, _remove_folder, args=(folder,))
    timer.daemon = True
    timer.start()
    return page.as_uri()


def _remove_folder(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def _sweep_old(tmp: Path, *, older_than_s: float) -> None:
    cutoff = time.time() - older_than_s
    try:
        stale = [p for p in tmp.glob(f"{_REDIRECT_PREFIX}*") if p.is_dir()]
    except OSError:
        return
    for p in stale:
        try:
            if p.stat().st_mtime < cutoff:
                _remove_folder(p)
        except OSError:
            continue


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
