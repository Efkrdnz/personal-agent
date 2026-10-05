"""The words every desktop backend speaks: what an app, a window and a volume are.

Kept apart from the backends so the tools, the pure matching code and three
platform implementations can share one vocabulary without any of them importing
another — and so a test's fake desktop is a class written against this file,
not against Windows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["App", "Desktop", "PcRefused", "PcStatus", "Volume", "Window"]


class PcRefused(RuntimeError):
    """The computer said no, or will not be asked. The message is a sentence to speak."""


@dataclass(frozen=True, slots=True)
class App:
    """Something the catalog found that can be launched.

    ``target`` came from the machine — a Start menu shortcut, an App Paths
    entry, an AppID, an allow-listed Settings page — and never from what the
    model said. ``display`` is what Jarvis says when opening it; an alias such as
    "bluetooth ayarları" is spoken as its page's English name.
    """

    name: str
    target: str
    source: str
    display: str = ""

    @property
    def spoken(self) -> str:
        return self.display or self.name


@dataclass(frozen=True, slots=True)
class Window:
    """One top-level window, as the taskbar would show it."""

    hwnd: int
    title: str
    pid: int
    #: The full path of the program that owns it, or "" when Windows would not say.
    exe: str
    #: The window class: what tells the desktop itself apart from a folder window.
    cls: str = ""
    #: True when the window belongs to Jarvis itself.
    own: bool = False


@dataclass(frozen=True, slots=True)
class Volume:
    """The output level as a percentage, and whether it is muted.

    ``exact`` is False when it could not be read back and is what was ASKED for,
    so a sentence built from it must say "about".
    """

    level: int
    muted: bool
    exact: bool = True


@dataclass(frozen=True, slots=True)
class PcStatus:
    available: bool
    backend: str
    detail: str


@runtime_checkable
class Desktop(Protocol):
    """What a tool may ask of the computer. Every refusal is a :class:`PcRefused`."""

    backend: str

    def status(self) -> PcStatus: ...

    def warm(self) -> None:
        """Start reading the app catalog in the background, so the first request does not wait."""

    def apps(self) -> tuple[App, ...]: ...

    def rescan(self) -> tuple[App, ...]:
        """Read the catalog again (an app installed a minute ago), unless it was read just now."""

    def launch(self, app: App) -> None: ...

    def open_url(self, url: str) -> None: ...

    def open_path(self, path: Path) -> None: ...

    def known_folder(self, place: str) -> Path: ...

    def windows(self) -> tuple[Window, ...]: ...

    def close(self, window: Window) -> None:
        """Ask the window to close, as its X button does. Never forced."""

    def wait_closed(self, window: Window, timeout_s: float) -> bool: ...

    def volume(self) -> Volume | None:
        """The level now, or None when it cannot be read."""

    def set_volume(self, level: int) -> Volume | None: ...

    def step_volume(self, delta: int) -> Volume | None: ...

    def set_mute(self, on: bool) -> Volume | None: ...

    def media(self, action: str) -> None: ...

    def lock(self) -> None: ...

    def power(self, action: str) -> None:
        """Shut down or restart NOW, asking every app to close and forcing none of them."""

    def sleep(self) -> None: ...

    def cancel_power(self) -> bool:
        """Abort a shutdown the operating system has pending. False when there was none."""

    def after(self, delay_s: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` once, ``delay_s`` from now, off the caller's thread."""
