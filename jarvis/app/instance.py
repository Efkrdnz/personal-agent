"""One Jarvis per user: the second double-click brings the window back instead.

Two copies would mean two desks fighting for one microphone and two schedulers
— harmless for the scheduler, whose claims are compare-and-swaps, but not for
the sound card. So the app takes an OS file lock before it starts anything, and
the copy that cannot get it reads where the running one is and opens its window.

THE LOCK IS THE TRUTH; ``app.json`` IS A HINT. The kernel releases a lock when
its holder dies, however it dies, so "is one running" is never stale. The JSON
only says where to find it, which is why it is cleared the moment a new holder
takes the lock — a file left by a crashed run would otherwise point the next
double-click at a port nobody serves.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TypeVar

from jarvis.app import paths
from jarvis.jobs import pid_is_gone

__all__ = ["Lock", "Running", "acquire", "bind_remembered", "hand_off", "publish", "running"]

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,}")

_T = TypeVar("_T")


@dataclass(frozen=True)
class Running:
    """Where the running copy serves its window."""

    pid: int
    port: int
    token: str

    @property
    def url(self) -> str:
        # The token in the fragment, exactly as the window server builds it:
        # never sent to the server, never in a log line.
        return f"http://127.0.0.1:{self.port}/#t={self.token}"


class Lock:
    """The held app lock. Released by :meth:`release`, or by the OS when this process ends."""

    def __init__(self, fh: IO[bytes], path: Path) -> None:
        self._fh = fh
        self.path = path

    def release(self) -> None:
        if self._fh.closed:
            return
        # The address goes first, while the lock still says only this copy may
        # touch it: a double-click during shutdown must start a new copy, not
        # open the window of one that is stopping.
        with suppress(OSError):
            (self.path.parent / "app.json").unlink()
        with suppress(OSError):
            _unlock(self._fh)
        self._fh.close()


def acquire(*, directory: Path | None = None) -> Lock | None:
    """Take the app lock, or None when another process holds it."""
    d = directory if directory is not None else paths.app_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "app.lock"
    fh = open(path, "a+b")  # noqa: SIM115 - held for the life of the app
    try:
        _lock(fh)
    except OSError:
        fh.close()
        return None
    # Whatever app.json says now was written by a copy that no longer holds
    # the lock, so it is wrong by definition.
    with suppress(OSError):
        (d / "app.json").unlink()
    return Lock(fh, path)


def publish(port: int, token: str, *, path: Path | None = None, pid: int | None = None) -> None:
    """Write ``app.json`` atomically, readable by this user only."""
    target = path if path is not None else paths.state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"pid": os.getpid() if pid is None else pid, "port": port, "token": token})
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    # 0o600 at creation, not chmod afterwards: the token is the whole of the
    # window's access check, and there must be no instant it is world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def running(
    *, path: Path | None = None, gone: Callable[[int], bool] = pid_is_gone
) -> Running | None:
    """The running copy from ``app.json``, or None if absent, garbled, or its pid is dead."""
    target = path if path is not None else paths.state_file()
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid, port, token = raw.get("pid"), raw.get("port"), raw.get("token")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        return None
    if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        return None
    if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
        return None
    if gone(pid):
        return None
    return Running(pid=pid, port=port, token=token)


def bind_remembered(make: Callable[[int], _T], *, path: Path | None = None) -> _T:
    """``make(port)`` on the port the window had last time, else on any free one.

    THE PORT IS PART OF THE PAGE'S ORIGIN. The browser keeps the page's own
    preferences — speak replies, the last tab — per origin, so a new port on
    every launch is a page that forgets them on every launch. Taken is not an
    error: something else has it now, and any free port still works.
    """
    target = path if path is not None else paths.app_dir() / "window.port"
    last = _remembered_port(target)
    server: _T | None = None
    if last is not None:
        with suppress(OSError):
            server = make(last)
    if server is None:
        server = make(0)
    port = getattr(server, "port", None)
    if isinstance(port, int) and port != last:
        with suppress(OSError):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(port), encoding="utf-8")
    return server


def _remembered_port(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    # Below 1024 is privileged or well known: never a port this app was given.
    if not text.isdigit() or not 1024 <= int(text) < 65536:
        return None
    return int(text)


def hand_off(
    open_window: Callable[[str], Any],
    *,
    find: Callable[[], Running | None] = running,
    wait_s: float = 3.0,
    every_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """What the second copy does: open the first one's window. Returns a log line.

    The first copy may hold the lock and not have published yet — it is in the
    second between taking the lock and starting its server — so an empty
    answer is waited out briefly rather than believed.
    """
    found = find()
    waited = 0.0
    while found is None and waited < wait_s:
        sleep(every_s)
        waited += every_s
        found = find()
    if found is None:
        return (
            "Jarvis is already running but has not said where its window is yet; "
            "try again in a moment."
        )
    open_window(found.url)
    return f"Jarvis is already running (pid {found.pid}); opened its window."


if sys.platform == "win32":
    import msvcrt

    def _lock(fh: IO[bytes]) -> None:
        # One byte at offset 0. LK_NBLCK fails at once instead of retrying for
        # ten seconds, which is what "is another copy running" needs.
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
