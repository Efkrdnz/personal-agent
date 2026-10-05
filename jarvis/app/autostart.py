"""Start Jarvis when the user signs in to Windows: one value under the HKCU Run key.

HKCU, not HKLM and not a scheduled task: it needs no administrator, it is the
same switch the Task Manager's "Startup apps" page shows and can turn off, and
it disappears with the user's profile.

Off Windows this is a no-op that says so. A Linux desktop has its own
autostart directory, but nobody has asked for it, and writing into a desktop
environment's config on a guess is worse than not doing it.
"""

from __future__ import annotations

import ntpath
import os
import sys
from typing import Any, Protocol

__all__ = ["RUN_KEY", "VALUE_NAME", "Registry", "command", "is_enabled", "set_enabled", "sync"]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Jarvis"


class Registry(Protocol):
    """The three operations this needs on the Run key; a dict-like fake in tests."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


def command(
    *, frozen: bool | None = None, executable: str | None = None, exists: Any = None
) -> str:
    """The command line Windows runs at sign-in. Quoted: ``Program Files`` has a space.

    Frozen, it is the exe itself. From a source install it is ``pythonw -m
    jarvis app`` — pythonw, because python.exe would open a console window at
    every sign-in, which is the thing this whole layer exists to avoid.
    """
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = sys.executable if executable is None else executable
    if frozen:
        return f'"{exe}"'
    exists = os.path.exists if exists is None else exists
    folder, name = ntpath.split(exe)
    windowed = exe
    if name.lower() == "python.exe":
        candidate = ntpath.join(folder, "pythonw.exe")
        if exists(candidate):
            windowed = candidate
    return f'"{windowed}" -m jarvis app'


def set_enabled(on: bool, *, platform: str | None = None, registry: Registry | None = None) -> str:
    """Turn start-with-Windows on or off. Returns what it did, as a sentence."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("win"):
        return "Starting with the computer is only available on Windows."
    reg = registry if registry is not None else _WinRun()
    if on:
        reg.set(VALUE_NAME, command())
        return "Jarvis will start when you sign in to Windows."
    reg.delete(VALUE_NAME)
    return "Jarvis will no longer start when you sign in."


def sync(wanted: bool, *, platform: str | None = None, registry: Registry | None = None) -> None:
    """Make the Run key match the setting, run once at every app start.

    The app is a folder the user unzipped, and the next version is unzipped
    somewhere else: a Run value written by the old one points at an exe that
    no longer exists, and Windows fails it silently at every sign-in.
    """
    platform = sys.platform if platform is None else platform
    if not platform.startswith("win"):
        return
    reg = registry if registry is not None else _WinRun()
    current = reg.get(VALUE_NAME)
    if wanted and current != command():
        reg.set(VALUE_NAME, command())
    elif not wanted and current is not None:
        reg.delete(VALUE_NAME)


def is_enabled(*, platform: str | None = None, registry: Registry | None = None) -> bool:
    platform = sys.platform if platform is None else platform
    if not platform.startswith("win"):
        return False
    reg = registry if registry is not None else _WinRun()
    return bool(reg.get(VALUE_NAME))


class _WinRun:
    """The real HKCU Run key. Imports winreg on use, so this module imports anywhere."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return str(value)

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass  # already off is off
