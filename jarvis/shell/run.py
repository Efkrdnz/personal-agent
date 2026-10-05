"""Run one command, hidden, bounded, and stoppable — and nothing else.

WHICH SHELL. PowerShell on Windows, by ABSOLUTE path: a bare ``powershell``
makes CreateProcess search the current directory first, and an app started at
login may be sitting in a folder anybody could have written a ``powershell.exe``
into. PowerShell 7 (``pwsh``) when it is installed, because it treats UTF-8 and
colour sanely; Windows PowerShell 5.1 from System32 otherwise, which every
Windows 10 and 11 machine has. ``/bin/sh -c`` everywhere else.

HOW IT RUNS:

* ``CREATE_NO_WINDOW`` on Windows. The app has no console, so a console
  program started from it gets its own black window over the user's work.
* Its own process group on POSIX (``start_new_session``), so the TREE can be
  killed: ``sh -c 'sleep 600 &'`` leaves a grandchild that killing the shell
  alone would orphan. On Windows ``taskkill /T /F`` (hidden) does the same job,
  because Python's ``kill`` there is TerminateProcess on one pid.
* In the home folder, not wherever the app happened to start — at login that is
  ``C:\\Windows\\System32``.
* With an environment that has had every Jarvis secret and PyInstaller's
  private variables taken out: a command the user asked for has no business
  with the Gemini key, and ``_MEIPASS2`` pointing at the app's unpacked files
  breaks any *other* PyInstaller program it starts.
* ``TERM=dumb`` and ``NO_COLOR=1``: measured, only ``TERM=dumb`` stops pwsh
  colouring its errors, and colour codes are noise to the model.

WAITING ON THE PROCESS, NOT ON THE PIPE. A command that starts something in
the background (``Start-Process``, ``cmd /c start``, ``&``) exits while the
thing it started still holds the output pipe open. Reading to EOF would then
hang until that program closes — possibly never. So the process is waited on,
a reader thread drains the pipe into a bounded buffer, and once the process
has gone the reader gets :data:`DRAIN_S` before the result is reported as
``incomplete``.

BOUNDED. The first :data:`HEAD_BYTES` and the last :data:`TAIL_BYTES` are kept
and the rest is counted: ``Get-ChildItem -Recurse C:\\`` must not cost the desk
its memory.
"""

from __future__ import annotations

import codecs
import ctypes
import ntpath
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from jarvis.secrets import SECRETS
from jarvis.shell.command import ENV_KEY, WRAPPER, clean, decode

__all__ = [
    "CREATE_NO_WINDOW",
    "DEFAULT_TIMEOUT_S",
    "MAX_TIMEOUT_S",
    "Outcome",
    "Shell",
    "ShellSpec",
    "code_pages",
    "kill_tree",
    "resolve",
    "run",
    "scrubbed_env",
]

CREATE_NO_WINDOW = 0x08000000

DEFAULT_TIMEOUT_S = 60
#: A tool call is held open while the command runs; ten minutes is already
#: longer than anybody should wait for an answer by voice.
MAX_TIMEOUT_S = 600

HEAD_BYTES = 256 * 1024
TAIL_BYTES = 64 * 1024
#: After the process exits, how long its output may take to arrive. Past this,
#: something it started is holding the pipe.
DRAIN_S = 2.0
#: Between asking the tree to stop and making it.
GRACE_S = 2.0
POLL_S = 0.25

#: PyInstaller's bootloader talks to its children through these. Inherited by a
#: command that starts another frozen program, they point it at OUR files.
_FROZEN_PREFIXES = ("_MEI", "_PYI")


@dataclass(frozen=True, slots=True)
class ShellSpec:
    """One shell: its fixed argv and how the command reaches it."""

    name: str
    #: How the read-back names it: "PowerShell", "the shell".
    spoken: str
    argv: tuple[str, ...]
    #: The environment variable the command travels in, or None to append it to argv.
    env_key: str | None = None

    def argv_for(self, command: str) -> list[str]:
        return list(self.argv) if self.env_key else [*self.argv, command]

    def env_for(self, command: str, base: Mapping[str, str]) -> dict[str, str]:
        env = {**base, "TERM": "dumb", "NO_COLOR": "1"}
        if self.env_key:
            env[self.env_key] = command
        return env


def resolve(
    platform: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.isfile,
) -> ShellSpec:
    """The shell for this machine. Every input injected, so Windows is testable here."""
    plat = platform or sys.platform
    env = os.environ if environ is None else environ
    if plat != "win32":
        return ShellSpec("sh", "the shell", ("/bin/sh", "-c"))
    exe = _pwsh(env, which, exists) or ntpath.join(
        env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows",
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    name = "pwsh" if ntpath.basename(exe).casefold() == "pwsh.exe" else "powershell"
    return ShellSpec(
        name,
        "PowerShell",
        # -OutputFormat Text: Windows PowerShell otherwise wraps errors written to
        # a redirected stream in CLIXML. Must come before -Command, which takes
        # the rest of the line.
        (
            exe,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-Command",
            WRAPPER,
        ),
        ENV_KEY,
    )


def _pwsh(
    env: Mapping[str, str], which: Callable[[str], str | None], exists: Callable[[str], bool]
) -> str | None:
    roots = [env.get(k) for k in ("ProgramFiles", "PROGRAMFILES", "ProgramW6432")]
    for root in (r for r in roots if r):
        candidate = ntpath.join(root, "PowerShell", "7", "pwsh.exe")
        if exists(candidate):
            return candidate
    found = which("pwsh.exe")
    # shutil.which on Windows tries the current directory first, and hands back
    # a RELATIVE path when it finds one there: exactly the hijack an absolute
    # path exists to prevent.
    if found and ntpath.isabs(found) and exists(found):
        return found
    return None


def scrubbed_env(
    environ: Mapping[str, str] | None = None, *, drop: Iterable[str] | None = None
) -> dict[str, str]:
    """The environment minus every Jarvis secret and PyInstaller's private variables."""
    env = os.environ if environ is None else environ
    names = {s.env.upper() for s in SECRETS} if drop is None else {d.upper() for d in drop}
    names.add(ENV_KEY)
    return {
        k: v
        for k, v in env.items()
        if k.upper() not in names and not k.upper().startswith(_FROZEN_PREFIXES)
    }


def code_pages(platform: str | None = None, *, kernel32: Any | None = None) -> tuple[str, ...]:
    """The console (OEM) code page, then the ANSI one, as Python codec names.

    OEM first: that is what console programs print in. Empty off Windows,
    where everything worth running prints UTF-8.
    """
    if (platform or sys.platform) != "win32":
        return ()
    k = kernel32 if kernel32 is not None else ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    pages: list[str] = []
    for getter in ("GetOEMCP", "GetACP"):
        try:
            number = int(getattr(k, getter)())
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        name = f"cp{number}"
        if number == 65001 or name in pages:
            continue  # UTF-8 is tried first anyway
        try:
            codecs.lookup(name)
        except LookupError:
            continue
        pages.append(name)
    return tuple(pages)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened. Bytes, not text: decoding needs the code pages, which the caller owns."""

    exit_code: int | None
    head: bytes = b""
    #: The end of the output, when the middle had to be dropped; else empty.
    tail: bytes = b""
    total_bytes: int = 0
    truncated: bool = False
    timed_out: bool = False
    #: Stopped because the kill switch moved, not because it ran out of time.
    stopped: bool = False
    #: Something the command started was still holding its output when it exited.
    incomplete: bool = False
    duration_s: float = 0.0
    pid: int | None = None
    #: Non-empty when it never started: the shell itself could not be launched.
    error: str = ""

    def text(self, fallbacks: tuple[str, ...] = ()) -> str:
        """Decoded and cleaned. A dropped middle is marked, never silently joined."""
        if not self.truncated:
            return clean(decode(self.head + self.tail, fallbacks=fallbacks))
        head = clean(decode(_whole_head(self.head), fallbacks=fallbacks))
        tail = clean(decode(_whole_tail(self.tail), fallbacks=fallbacks))
        dropped = self.total_bytes - len(self.head) - len(self.tail)
        return f"{head}\n[… {dropped} bytes of output not kept …]\n{tail}"


def _whole_head(data: bytes) -> bytes:
    """Cut a byte slice back to a UTF-8 character boundary, so strict decoding can work."""
    for back in range(1, min(4, len(data)) + 1):
        b = data[-back]
        if b < 0x80:
            return data
        if b >= 0xC0:  # a lead byte: is its sequence complete?
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return data if back >= need else data[:-back]
    return data


def _whole_tail(data: bytes) -> bytes:
    i = 0
    while i < min(3, len(data)) and 0x80 <= data[i] < 0xC0:
        i += 1
    return data[i:]


@dataclass
class _Capture:
    """The reader thread's buffer: the head kept whole, the tail as a sliding window."""

    head_max: int
    tail_max: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def drain(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        read = getattr(stream, "read1", None) or stream.read
        with suppress(OSError, ValueError):
            while chunk := read(65536):
                self.feed(chunk)

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            room = self.head_max - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > self.tail_max:
                    del self.tail[: len(self.tail) - self.tail_max]

    def snapshot(self) -> tuple[bytes, bytes, int]:
        with self._lock:
            return bytes(self.head), bytes(self.tail), self.total


def _spawn(argv: list[str], *, cwd: str, env: dict[str, str], platform: str) -> Any:
    """The one place a command's process is created. Hidden on Windows, its own group elsewhere."""
    return subprocess.Popen(  # noqa: S603 - argv is a fixed shell and the checked command
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env,
        creationflags=CREATE_NO_WINDOW if platform == "win32" else 0,
        # POSIX only (ignored on Windows): the command leads its own process
        # group, so the whole tree can be signalled and ours never is.
        start_new_session=platform != "win32",
    )


def _taskkill(pid: int) -> None:
    # Windows environment names are case-insensitive; os.environ there is too.
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    exe = ntpath.join(root, "System32", "taskkill.exe")
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - a fixed system binary and a pid
            [exe, "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )


def kill_tree(
    proc: Any,
    *,
    platform: str | None = None,
    grace_s: float = GRACE_S,
    taskkill: Callable[[int], None] = _taskkill,
    killpg: Callable[[int, int], None] | None = None,
) -> None:
    """Stop the command and everything it started. Never raises."""
    plat = platform or sys.platform
    if plat == "win32":
        taskkill(proc.pid)
        with suppress(OSError):
            proc.kill()
        return
    signal_group = killpg or os.killpg
    with suppress(ProcessLookupError, PermissionError):
        signal_group(proc.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace_s)
    # Unconditionally: the shell may have died on SIGTERM while a grandchild
    # that ignores it is still running in the group.
    with suppress(ProcessLookupError, PermissionError):
        signal_group(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def _never() -> bool:
    return False


def _asked_to_stop(should_stop: Callable[[], bool]) -> bool:
    # A kill switch that cannot be read (a locked database) does not stop the
    # command; the timeout still bounds it. Raising here would orphan the tree.
    try:
        return bool(should_stop())
    except Exception:  # noqa: BLE001 - see above
        return False


def run(
    spec: ShellSpec,
    command: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_s: float,
    should_stop: Callable[[], bool] = _never,
    popen: Callable[..., Any] = _spawn,
    tree_kill: Callable[[Any], None] | None = None,
    platform: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = POLL_S,
    head_bytes: int = HEAD_BYTES,
    tail_bytes: int = TAIL_BYTES,
    drain_s: float = DRAIN_S,
) -> Outcome:
    """Run ``command`` and wait for it, the timeout, or ``should_stop``. Never raises."""
    plat = platform or sys.platform
    start = clock()
    try:
        proc = popen(spec.argv_for(command), cwd=cwd, env=spec.env_for(command, env), platform=plat)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Outcome(exit_code=None, error=f"{type(exc).__name__}: {exc}")

    sink = _Capture(head_bytes, tail_bytes)
    reader = threading.Thread(
        target=sink.drain, args=(proc.stdout,), name="jarvis-shell-output", daemon=True
    )
    reader.start()
    stop = tree_kill or (lambda p: kill_tree(p, platform=plat))
    timed_out = stopped = False
    while proc.poll() is None:
        if clock() - start >= timeout_s:
            timed_out = True
        elif _asked_to_stop(should_stop):
            stopped = True
        if timed_out or stopped:
            stop(proc)
            break
        sleep(poll_s)
    code: int | None
    try:
        code = proc.wait(timeout=GRACE_S * 2)
    except subprocess.TimeoutExpired:
        code = None
    duration = clock() - start
    reader.join(drain_s)
    incomplete = reader.is_alive()
    head, tail, total = sink.snapshot()
    return Outcome(
        exit_code=code,
        head=head,
        tail=tail,
        total_bytes=total,
        truncated=total > len(head) + len(tail),
        timed_out=timed_out,
        stopped=stopped,
        incomplete=incomplete,
        duration_s=round(duration, 3),
        pid=getattr(proc, "pid", None),
    )


@dataclass(frozen=True)
class Shell:
    """This machine's shell, ready to run one command: what a tool holds."""

    spec: ShellSpec
    #: Code pages to try after UTF-8 when decoding what it printed.
    fallbacks: tuple[str, ...] = ()
    #: Where commands run. Empty: the user's home folder, read at run time.
    cwd: str = ""
    platform: str = field(default_factory=lambda: sys.platform)

    @classmethod
    def here(cls) -> Shell:
        plat = sys.platform
        return cls(resolve(plat), code_pages(plat), platform=plat)

    @property
    def spoken(self) -> str:
        return self.spec.spoken

    def run(
        self,
        command: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        should_stop: Callable[[], bool] = _never,
    ) -> Outcome:
        return run(
            self.spec,
            command,
            cwd=self.cwd or str(Path.home()),
            env=scrubbed_env(),
            timeout_s=timeout_s,
            should_stop=should_stop,
            platform=self.platform,
        )

    def text(self, outcome: Outcome) -> str:
        return outcome.text(self.fallbacks)
