"""This computer, by voice: open, close, volume, media, lock, sleep and shut down.

A PLATFORM LAYER, BELOW THE TOOLS. ``jarvis/tools/builtin/pc.py`` decides what a
sentence may do and who must say yes; this package only knows how Windows (or
Linux, or macOS) does it. It is standard library only — ctypes, winreg,
``os.startfile`` and hidden subprocesses — and imports under ``python -S``, so
the platform half can never pull a dependency into a process that does not
need it, and every operating-system call is injectable: the tests drive the
Windows code on Linux with fakes, and the Windows CI job runs the real thing.

Modules, in the order a request meets them:

``catalog``  which installed app a spoken name means (pure, Turkish-aware)
``safety``   which URLs, files and windows may be opened or closed (pure)
``windows``  WindowsDesktop: the Start menu, the shell, user32, Core Audio, shutdown
``posix``    PosixDesktop: best effort with xdg-open, wpctl, playerctl, loginctl
``null``     NullDesktop: every request refused with one sentence

:func:`choose_desktop` picks one. Building it is cheap and reads nothing; the
app catalog is read on first use or by :meth:`Desktop.warm`.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping

from jarvis.pc.base import App, Desktop, PcRefused, PcStatus, Volume, Window
from jarvis.pc.null import NullDesktop

__all__ = [
    "App",
    "Desktop",
    "NullDesktop",
    "PcRefused",
    "PcStatus",
    "Volume",
    "Window",
    "available",
    "choose_desktop",
]


def choose_desktop(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> Desktop:
    """The backend for this machine. Never raises: an unusable one is a NullDesktop."""
    plat = sys.platform if platform is None else platform
    environ = os.environ if env is None else env
    if plat == "win32":
        from jarvis.pc.windows import WindowsDesktop

        return WindowsDesktop(env=environ)
    if plat.startswith("linux") or plat == "darwin":
        from jarvis.pc.posix import PosixDesktop

        posix = PosixDesktop(platform=plat, env=environ, which=which or shutil.which)
        status = posix.status()
        return posix if status.available else NullDesktop(status.detail)
    return NullDesktop("I can't control this kind of computer.")


def available(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> bool:
    """Whether the PC tools can do anything here. The composition root withholds them if not."""
    return choose_desktop(platform=platform, env=env, which=which).status().available
