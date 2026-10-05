"""Linux and macOS: best effort, through the desktop's own command-line tools.

The user this is built for runs Windows; this backend exists so the same tools
behave sensibly on a developer's Linux box and on CI, and so "not available
here" is a sentence rather than a crash. What a machine lacks (no wpctl, no
playerctl, no display) is a :class:`PcRefused` naming the missing piece.

Commands are run with an argv list, never a shell, and only with arguments the
code built: a catalog entry's desktop-file id, an http(s) URL that passed
:func:`jarvis.pc.safety.safe_url`, a path resolved inside a known folder.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from jarvis.pc.base import App, PcRefused, PcStatus, Volume, Window
from jarvis.pc.catalog import Catalog, launchable
from jarvis.pc.safety import is_executable, safe_url

__all__ = ["PosixDesktop", "parse_desktop_entry", "parse_pactl", "parse_wpctl", "user_dirs"]

_XDG_KEYS = {
    "desktop": "XDG_DESKTOP_DIR",
    "documents": "XDG_DOCUMENTS_DIR",
    "downloads": "XDG_DOWNLOAD_DIR",
    "pictures": "XDG_PICTURES_DIR",
    "music": "XDG_MUSIC_DIR",
    "videos": "XDG_VIDEOS_DIR",
}
_DEFAULT_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}
_MEDIA = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}
_SINK = "@DEFAULT_AUDIO_SINK@"


def parse_desktop_entry(text: str) -> str | None:
    """The Name of a launchable .desktop entry, or None for a hidden or non-app one. Pure."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]
    if entry.get("Type", "Application") != "Application":
        return None
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return None
    name = entry.get("Name", "").strip()
    return name or None


def parse_wpctl(text: str) -> Volume | None:
    """``Volume: 0.40 [MUTED]`` -> Volume(40, True). Pure."""
    m = re.search(r"Volume:\s*([0-9.]+)", text or "")
    if not m:
        return None
    return Volume(level=round(float(m.group(1)) * 100), muted="MUTED" in text)


def parse_pactl(volume_text: str, mute_text: str) -> Volume | None:
    """``pactl get-sink-volume`` and ``get-sink-mute`` output. Pure."""
    m = re.search(r"(\d+)%", volume_text or "")
    if not m:
        return None
    return Volume(level=int(m.group(1)), muted=bool(re.search(r"Mute:\s*yes", mute_text or "")))


def user_dirs(text: str, home: Path) -> dict[str, Path]:
    """``~/.config/user-dirs.dirs`` -> {"downloads": Path(...)}. Pure."""
    out: dict[str, Path] = {}
    keys = {v: k for k, v in _XDG_KEYS.items()}
    for line in (text or "").splitlines():
        m = re.match(r'\s*(XDG_\w+_DIR)\s*=\s*"(.*)"\s*$', line)
        if m and m.group(1) in keys:
            out[keys[m.group(1)]] = Path(m.group(2).replace("$HOME", str(home)))
    return out


def _timer(delay_s: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay_s, fn)
    t.daemon = True
    t.start()


class PosixDesktop:
    """The Desktop for Linux (X11 or Wayland session) and macOS."""

    def __init__(
        self,
        *,
        platform: str = "linux",
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., Any] = subprocess.run,
        spawn: Callable[..., Any] = subprocess.Popen,
        after: Callable[[float, Callable[[], None]], None] = _timer,
        home: Path | None = None,
        data_dirs: Iterable[Path] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.mac = platform == "darwin"
        self.backend = "macos" if self.mac else "linux"
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._which = which
        self._run = run
        self._spawn = spawn
        self._after = after
        self._home = home or Path(self._env.get("HOME") or Path.home())
        self._data_dirs = list(data_dirs) if data_dirs is not None else None
        self._clock = clock
        self._sleep = sleep
        self.catalog = Catalog({"apps": self._scan})

    # ── plumbing ──

    def status(self) -> PcStatus:
        if not self.mac and not (self._env.get("DISPLAY") or self._env.get("WAYLAND_DISPLAY")):
            return PcStatus(
                False, self.backend, "There's no desktop session here for me to control."
            )
        return PcStatus(True, self.backend, f"{self.backend} desktop, best effort")

    def _tool(self, *names: str) -> str:
        for name in names:
            if self._which(name):
                return name
        raise PcRefused(f"That needs {' or '.join(names)}, which isn't installed here.")

    def _call(self, argv: list[str], *, timeout: float = 10.0) -> str:
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PcRefused(f"{argv[0]} didn't run: {exc}.") from exc
        if proc.returncode != 0:
            why = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise PcRefused(f"{argv[0]} said no: {why[0] if why else proc.returncode}.")
        return str(proc.stdout or "")

    def _detach(self, argv: list[str]) -> None:
        # Detached: xdg-open can block until the app it started exits.
        try:
            self._spawn(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise PcRefused(f"{argv[0]} didn't run: {exc}.") from exc

    # ── apps ──

    def _dirs(self) -> list[Path]:
        if self._data_dirs is not None:
            return self._data_dirs
        if self.mac:
            return [
                Path("/Applications"),
                Path("/System/Applications"),
                self._home / "Applications",
            ]
        data_home = self._env.get("XDG_DATA_HOME") or str(self._home / ".local" / "share")
        dirs = [
            data_home,
            *(self._env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"),
        ]
        return [Path(d) / "applications" for d in dirs if d]

    def _scan(self) -> list[App]:
        out: list[App] = []
        for d in self._dirs():
            if not d.is_dir():
                continue
            if self.mac:
                out += [App(p.stem, str(p), "macapp") for p in sorted(d.glob("*.app"))]
                continue
            for p in sorted(d.glob("*.desktop")):
                try:
                    name = parse_desktop_entry(p.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if name:
                    out.append(App(name, p.name, "desktop"))
        return out

    def warm(self) -> None:
        self.catalog.warm()

    def apps(self) -> tuple[App, ...]:
        return self.catalog.apps()

    def rescan(self) -> tuple[App, ...]:
        return self.catalog.rescan()

    def launch(self, app: App) -> None:
        if not launchable(app) or app not in self.apps():
            raise PcRefused("I can only open apps I found installed on this computer.")
        if self.mac:
            self._detach(["open", "-a", app.target])
        else:
            self._detach([self._tool("gtk-launch"), app.target.removesuffix(".desktop")])

    def open_url(self, url: str) -> None:
        self._detach(["open" if self.mac else self._tool("xdg-open"), safe_url(url)])

    def open_path(self, path: Path) -> None:
        if not path.exists():
            raise PcRefused(f"I can't find {path.name}.")
        if path.is_file() and is_executable(path.name, ""):
            raise PcRefused("I won't open programs, scripts or shortcuts from a folder.")
        self._detach(["open" if self.mac else self._tool("xdg-open"), str(path)])

    def known_folder(self, place: str) -> Path:
        if place == "home":
            return self._home
        if place not in _DEFAULT_NAMES:
            raise PcRefused(f"I don't know a folder called {place}.")
        if not self.mac:
            cfg = (
                Path(self._env.get("XDG_CONFIG_HOME") or self._home / ".config") / "user-dirs.dirs"
            )
            try:
                found = user_dirs(cfg.read_text(encoding="utf-8"), self._home).get(place)
            except OSError:
                found = None
            if found is not None:
                return found
        return self._home / _DEFAULT_NAMES[place]

    # ── windows ──

    def windows(self) -> tuple[Window, ...]:
        if self.mac:
            raise PcRefused("I can't see the windows on a Mac yet.")
        out = []
        for line in self._call([self._tool("wmctrl"), "-lp"]).splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5 or not parts[2].isdigit():
                continue
            hwnd, pid, title = int(parts[0], 16), int(parts[2]), parts[4]
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = ""
            out.append(Window(hwnd, title, pid, exe, own=pid == os.getpid()))
        return tuple(out)

    def close(self, window: Window) -> None:
        self._call([self._tool("wmctrl"), "-i", "-c", hex(window.hwnd)])

    def wait_closed(self, window: Window, timeout_s: float) -> bool:
        deadline = self._clock() + timeout_s
        while True:
            if all(w.hwnd != window.hwnd for w in self.windows()):
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(0.25)

    # ── sound ──

    def volume(self) -> Volume | None:
        try:
            if self.mac:
                level = self._call(["osascript", "-e", "output volume of (get volume settings)"])
                muted = self._call(["osascript", "-e", "output muted of (get volume settings)"])
                return Volume(int(level.strip() or 0), muted.strip() == "true")
            if self._which("wpctl"):
                return parse_wpctl(self._call(["wpctl", "get-volume", _SINK]))
            tool = self._tool("pactl")
            return parse_pactl(
                self._call([tool, "get-sink-volume", "@DEFAULT_SINK@"]),
                self._call([tool, "get-sink-mute", "@DEFAULT_SINK@"]),
            )
        except (PcRefused, ValueError):
            return None

    def set_volume(self, level: int) -> Volume | None:
        level = min(100, max(0, int(level)))
        if self.mac:
            self._call(["osascript", "-e", f"set volume output volume {level}"])
        elif self._which("wpctl"):
            self._call(["wpctl", "set-volume", _SINK, f"{level / 100:.2f}"])
            if level > 0:
                self._call(["wpctl", "set-mute", _SINK, "0"])
        else:
            self._call([self._tool("pactl"), "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
        return self.volume()

    def step_volume(self, delta: int) -> Volume | None:
        now = self.volume()
        if now is None:
            raise PcRefused("I can't read the volume here, so I can't change it by steps.")
        return self.set_volume(now.level + delta)

    def set_mute(self, on: bool) -> Volume | None:
        flag = "1" if on else "0"
        if self.mac:
            self._call(["osascript", "-e", f"set volume output muted {str(on).lower()}"])
        elif self._which("wpctl"):
            self._call(["wpctl", "set-mute", _SINK, flag])
        else:
            self._call([self._tool("pactl"), "set-sink-mute", "@DEFAULT_SINK@", flag])
        return self.volume()

    def media(self, action: str) -> None:
        if action not in _MEDIA:
            raise PcRefused("I can play or pause, skip, go back or stop.")
        self._call([self._tool("playerctl"), _MEDIA[action]])

    # ── session and power ──

    def lock(self) -> None:
        if self.mac:
            raise PcRefused("I can't lock a Mac yet.")
        self._call([self._tool("loginctl"), "lock-session"])

    def power(self, action: str) -> None:
        if action not in ("shutdown", "restart"):
            raise PcRefused("I can shut down or restart.")
        if self.mac:
            verb = "shut down" if action == "shutdown" else "restart"
            # System Events asks every app, as the menu item does; nothing is forced.
            self._call(["osascript", "-e", f'tell application "System Events" to {verb}'])
            return
        # `systemctl poweroff` ends the session's processes without asking them,
        # which is the force this whole feature refuses. GNOME's own logout asks.
        if not self._which("gnome-session-quit"):
            raise PcRefused(
                "On this desktop I can't shut down without forcing apps closed, so I won't."
            )
        flag = "--power-off" if action == "shutdown" else "--reboot"
        self._detach(["gnome-session-quit", flag])

    def sleep(self) -> None:
        if self.mac:
            self._call(["pmset", "sleepnow"])
            return
        self._call([self._tool("systemctl"), "suspend"])

    def cancel_power(self) -> bool:
        # The countdown is Jarvis's own; nothing is pending at the OS level here.
        return False

    def after(self, delay_s: float, fn: Callable[[], None]) -> None:
        self._after(delay_s, fn)
