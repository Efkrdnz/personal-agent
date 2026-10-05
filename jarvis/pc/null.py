"""The desktop that is not there: every request refused with one sentence.

One code path for callers, like ``jarvis.capture.NullCapturer``. A headless box,
an SSH session or an unknown platform still gets the PC tools' refusals as
sentences, and never an AttributeError from a backend that was not built.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from jarvis.pc.base import App, PcRefused, PcStatus, Volume, Window

__all__ = ["NullDesktop"]


class NullDesktop:
    backend = "none"

    def __init__(self, reason: str = "I can't control this computer from here.") -> None:
        self.reason = reason

    def _no(self) -> NoReturn:
        raise PcRefused(self.reason)

    def status(self) -> PcStatus:
        return PcStatus(available=False, backend=self.backend, detail=self.reason)

    def warm(self) -> None:
        """Nothing to read."""

    def apps(self) -> tuple[App, ...]:
        self._no()

    def rescan(self) -> tuple[App, ...]:
        self._no()

    def launch(self, app: App) -> None:
        self._no()

    def open_url(self, url: str) -> None:
        self._no()

    def open_path(self, path: Path) -> None:
        self._no()

    def known_folder(self, place: str) -> Path:
        self._no()

    def windows(self) -> tuple[Window, ...]:
        self._no()

    def close(self, window: Window) -> None:
        self._no()

    def wait_closed(self, window: Window, timeout_s: float) -> bool:
        self._no()

    def volume(self) -> Volume | None:
        self._no()

    def set_volume(self, level: int) -> Volume | None:
        self._no()

    def step_volume(self, delta: int) -> Volume | None:
        self._no()

    def set_mute(self, on: bool) -> Volume | None:
        self._no()

    def media(self, action: str) -> None:
        self._no()

    def lock(self) -> None:
        self._no()

    def power(self, action: str) -> None:
        self._no()

    def sleep(self) -> None:
        self._no()

    def cancel_power(self) -> bool:
        self._no()

    def after(self, delay_s: float, fn: Callable[[], None]) -> None:
        self._no()
