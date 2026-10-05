"""Start the desk, the scheduler and the Telegram bot, and keep them running.

Each is still its own OS process, exactly as from a terminal: the desk is
designed to crash alone, and a runner to be killed and resumed. What changes is
who starts them. A person who double-clicked an icon cannot read a refusal
printed to a console that does not exist, so this module owns three things a
terminal used to do for free:

* WHERE THE OUTPUT GOES. Each child's stdout and stderr are read by two small
  threads and appended to its own log file. Pipes rather than handing the child
  the file itself: on Windows a handle opened for append by one process and
  written by another shares a file position, and lines overwrite each other.
  The parent as the only writer cannot get that wrong, and it is also the only
  way to know which lines came from STDERR — which is where every refusal is.
* WHAT AN EXIT MEANS. Exit 2 is the house convention for "refused to start, and
  I said why" (no key, no microphone, no wake model). Restarting that is a loop
  that can never succeed, so it is HELD with the last stderr lines as the
  reason until somebody fixes the cause and asks again. Any other exit is a
  crash, restarted with a backoff so a crash at startup costs one line a
  minute, not a hundred a second.
* THAT NOTHING OUTLIVES THE APP. On Windows every child goes into a job object
  that kills it when the app's last handle closes — including when the app is
  ended from Task Manager. Otherwise a second double-click starts a second desk
  while the orphan still holds the microphone.

Nothing here knows what a desk is: specs are argv tuples, and what an exit
means for the database is the ``on_exit`` callback the composition root passes.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from jarvis import bus, liveness
from jarvis.app import paths

__all__ = [
    "BACKOFF_S",
    "CREATE_NO_WINDOW",
    "EXIT_REFUSED",
    "REASON_CHARS",
    "STABLE_S",
    "Control",
    "ProcessSpec",
    "Supervisor",
    "app_specs",
    "backoff",
    "boot_names",
    "exit_recorder",
    "last_burst",
    "poll_forever",
    "reason_from",
]

#: Windows: start a console program without a console. Without it every child
#: of a windowed app flashes a black window, which is the exact thing the user
#: asked never to see.
CREATE_NO_WINDOW = 0x08000000

#: "I refused, and stderr says why." See the module docstring.
EXIT_REFUSED = 2

#: Seconds before the Nth consecutive restart. Capped, never exhausted: the
#: thing a crash loop usually waits for — a network, a sound card plugged back
#: in — arrives on its own, and a supervisor that gave up would miss it.
BACKOFF_S: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)

#: A run this long was not a crash loop, so the next crash starts the backoff
#: from the bottom again.
STABLE_S = 60.0

#: The reason shown in the HUD. Enough for a sentence and its fix.
REASON_CHARS = 400

# A refusal is one print() of a few lines, so it reaches the pipe as one burst.
# Lines further apart than this belong to something said earlier — a library
# warning at startup — and are not the reason.
_BURST_GAP_S = 0.5
# At most this many lines of that burst; past it, the start is the headline.
_REASON_LINES = 8
_KEEP_LINES = 40
# A pump still reading after its child exited means a grandchild inherited the
# pipe. Waiting for it longer than this would stall the poll thread.
_PUMP_JOIN_S = 2.0
_LOG_TAIL_BYTES = 4096


@dataclass(frozen=True)
class ProcessSpec:
    """One child: its liveness name, and the arguments after the interpreter."""

    name: str
    argv: tuple[str, ...]
    restart: bool = True


def backoff(failures: int) -> float:
    """Seconds to wait before restart number ``failures`` (1-based) of a crash streak."""
    return BACKOFF_S[max(0, min(failures, len(BACKOFF_S)) - 1)]


def reason_from(lines: Iterable[str]) -> str:
    """The sentence a held or crashed process leaves behind, from its last stderr lines.

    A Python traceback is reduced to its final line, which is the exception and
    its message; the frames above it are for the log, not for a person. Lines
    stay lines: the window shows a multi-line refusal by its first.
    """
    kept = [ln.rstrip() for ln in lines if ln.strip()]
    if not kept:
        return ""
    if any(ln.startswith("Traceback (most recent call last)") for ln in kept):
        return _clip(kept[-1].strip())
    return _clip("\n".join(ln.strip() for ln in kept[:_REASON_LINES]))


def last_burst(entries: Iterable[tuple[float, str]], gap_s: float = _BURST_GAP_S) -> list[str]:
    """The lines that arrived together at the end: one message, not the whole stderr."""
    timed = list(entries)
    if not timed:
        return []
    start = len(timed) - 1
    while start > 0 and timed[start][0] - timed[start - 1][0] <= gap_s:
        start -= 1
    return [line for _, line in timed[start:]]


def _clip(text: str) -> str:
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 1] + "…"


class _Sink:
    """A log file both pumps of one child write whole lines to."""

    def __init__(self, path: Path) -> None:
        self._fh: IO[bytes] = open(path, "ab")  # noqa: SIM115 - closed by the reaper
        self._lock = threading.Lock()

    def offset(self) -> int:
        with self._lock:
            return self._fh.tell()

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._fh.closed:
                return
            self._fh.write(data)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def _pump(stream: IO[bytes], sink: _Sink, tail: deque[tuple[float, str]] | None) -> None:
    try:
        for raw in iter(stream.readline, b""):
            sink.write(raw)
            if tail is not None:
                tail.append((time.monotonic(), raw.decode("utf-8", "replace")))
    except (OSError, ValueError):
        pass  # the pipe or the log closed under us: the run is over either way
    finally:
        with suppress(OSError, ValueError):
            stream.close()


@dataclass
class _Child:
    spec: ProcessSpec
    proc: Any = None
    started_at: float | None = None
    runs: int = 0
    restarts: int = 0
    last_exit: int | None = None
    held: bool = False
    reason: str = ""
    #: Should be running: start() and restart() set it, stop() clears it.
    wanted: bool = False
    #: The current run was asked to end, so its exit is not a crash.
    stopping: bool = False
    next_start: float | None = None
    failures: int = 0
    log: str = ""
    log_offset: int = 0
    sink: _Sink | None = None
    pumps: tuple[threading.Thread, ...] = ()
    stderr_tail: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=_KEEP_LINES))


class Supervisor:
    """Runs a fixed set of child processes. Every method is safe from any thread."""

    def __init__(
        self,
        specs: Iterable[ProcessSpec],
        *,
        python: str = sys.executable,
        env: Mapping[str, str] | None = None,
        log_path: Callable[[str], Path] = paths.log_path,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        on_exit: Callable[[str, int, str], None] | None = None,
        platform: str = sys.platform,
    ) -> None:
        self._children: dict[str, _Child] = {}
        for spec in specs:
            if spec.name in self._children:
                raise ValueError(f"two processes are called {spec.name!r}")
            self._children[spec.name] = _Child(spec)
        self._python = python
        self._env = dict(os.environ if env is None else env)
        self._log_path = log_path
        self._popen = popen
        self._clock = clock
        self._on_exit = on_exit
        self._windows = platform.startswith("win")
        self._job: _WinJob | None = None
        # Re-entrant: _spawn runs under it and is reached from start(),
        # restart() and poll() alike.
        self._lock = threading.RLock()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._children)

    # -- the four verbs -----------------------------------------------------

    def start(self, name: str | None = None) -> None:
        """Start one process, or all. A HELD process stays held: see :meth:`restart`."""
        with self._lock:
            for c in self._select(name):
                if c.held:
                    continue
                c.wanted = True
                if c.proc is None:
                    self._spawn(c)

    def stop(self, name: str | None = None, timeout: float = 5.0) -> None:
        """Terminate one process or all, and wait for them.

        Every target is signalled before any is waited for, so stopping three
        processes takes one timeout, not three.
        """
        targets: list[tuple[_Child, Any, tuple[threading.Thread, ...]]] = []
        with self._lock:
            for c in self._select(name):
                c.wanted = False
                c.next_start = None
                if c.proc is not None:
                    c.stopping = True
                    targets.append((c, c.proc, c.pumps))
        for _, proc, _ in targets:
            # terminate() is TerminateProcess on Windows and SIGTERM on POSIX;
            # SIGKILL follows below for a child that ignores SIGTERM.
            with suppress(OSError):
                proc.terminate()
        deadline = time.monotonic() + timeout
        for c, proc, pumps in targets:
            try:
                code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    proc.kill()
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    continue  # unkillable; poll() reaps it if it ever goes
            self._finish(c, proc, code, pumps)

    def restart(self, name: str) -> None:
        """Stop it if it runs, then start it now. Also the only way to clear a hold."""
        if name not in self._children:
            raise KeyError(f"no process called {name!r}")
        with self._lock:
            c = self._children[name]
            c.held = False
            c.failures = 0
            c.reason = ""
        self.stop(name)
        with self._lock:
            c.wanted = True
            if c.proc is None:
                self._spawn(c)

    def poll(self) -> None:
        """Reap what has exited and start what is due. Call it about once a second."""
        exited: list[tuple[_Child, Any, int, tuple[threading.Thread, ...]]] = []
        with self._lock:
            for c in self._children.values():
                if c.proc is not None:
                    code = c.proc.poll()
                    if code is not None:
                        exited.append((c, c.proc, code, c.pumps))
        for c, proc, code, pumps in exited:
            self._finish(c, proc, code, pumps)
        with self._lock:
            now = self._clock()
            for c in self._children.values():
                due = c.next_start is not None and now >= c.next_start
                if c.proc is None and c.wanted and not c.held and due:
                    self._spawn(c)

    def status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for name, c in self._children.items():
                running = c.proc is not None and c.proc.poll() is None
                out[name] = {
                    "running": running,
                    "pid": c.proc.pid if running else None,
                    "restarts": c.restarts,
                    "last_exit": c.last_exit,
                    "held": c.held,
                    "reason": c.reason,
                    "log": c.log,
                }
            return out

    # -- inside -------------------------------------------------------------

    def _select(self, name: str | None) -> list[_Child]:
        if name is None:
            return list(self._children.values())
        if name not in self._children:
            raise KeyError(f"no process called {name!r}")
        return [self._children[name]]

    def _spawn(self, c: _Child) -> None:
        """Start ``c`` now. Caller holds the lock."""
        c.next_start = None
        c.stopping = False
        path = self._log_path(c.spec.name)
        c.log = str(path)
        sink = _Sink(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        sink.write(f"\n--- {stamp} starting {c.spec.name}: {' '.join(c.spec.argv)}\n".encode())
        c.log_offset = sink.offset()
        c.stderr_tail = deque(maxlen=_KEEP_LINES)
        env = {
            **self._env,
            # The child's own text is UTF-8 whatever the Windows code page is,
            # and unbuffered so a crash leaves every line before it in the log.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "JARVIS_APP": "1",
            # Where the child writes if its interpreter gives it no stdout at
            # all — a windowed build does that. See jarvis.app.entry.
            "JARVIS_LOG": str(path),
        }
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "close_fds": True,
        }
        if self._windows:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            # Its own session: a Ctrl+C in the terminal that started the app
            # reaches the app, which stops its children in order, rather than
            # reaching every child at once and racing the restart logic.
            kwargs["start_new_session"] = True
        argv = [self._python, *c.spec.argv]
        try:
            proc = self._popen(argv, **kwargs)
        except (OSError, ValueError) as exc:
            sink.write(f"--- could not start: {exc}\n".encode())
            sink.close()
            c.reason = f"could not start: {exc}"
            c.failures += 1
            c.next_start = self._clock() + backoff(c.failures)
            return
        c.proc = proc
        c.started_at = self._clock()
        if c.runs:
            c.restarts += 1
        c.runs += 1
        c.sink = sink
        pumps = []
        for stream, tail in ((proc.stdout, None), (proc.stderr, c.stderr_tail)):
            if stream is None:
                continue
            t = threading.Thread(
                target=_pump, args=(stream, sink, tail), name=f"log-{c.spec.name}", daemon=True
            )
            t.start()
            pumps.append(t)
        c.pumps = tuple(pumps)
        if self._windows:
            self._contain(proc)

    def _finish(self, c: _Child, proc: Any, code: int, pumps: tuple[threading.Thread, ...]) -> None:
        # Outside the lock: the pumps may still be writing the last lines, and
        # those lines ARE the reason.
        for t in pumps:
            t.join(_PUMP_JOIN_S)
        with self._lock:
            if c.proc is not proc:
                return  # another thread reaped this run first
            c.proc = None
            c.last_exit = code
            # A copy: a pump that outlived its join can still be appending.
            tail = reason_from(last_burst(list(c.stderr_tail)))
            if c.sink is not None:
                if not tail:
                    tail = reason_from(_log_lines(Path(c.log), c.log_offset)[-_REASON_LINES:])
                c.sink.write(f"--- exited {code}\n".encode())
                c.sink.close()
                c.sink = None
            now = self._clock()
            ran = now - (c.started_at if c.started_at is not None else now)
            if c.stopping:
                c.stopping = False
                c.reason = "stopped"
            elif code == EXIT_REFUSED:
                c.held = True
                c.next_start = None
                c.reason = tail or "it refused to start; its log says why"
            else:
                c.reason = "" if code == 0 else (tail or f"it stopped unexpectedly (exit {code})")
                if c.spec.restart and c.wanted:
                    c.failures = c.failures + 1 if ran < STABLE_S else 1
                    c.next_start = now + backoff(c.failures)
                else:
                    c.wanted = False
            name, reason = c.spec.name, c.reason
        if self._on_exit is not None:
            try:
                self._on_exit(name, code, reason)
            except Exception:  # noqa: BLE001 - a broken callback must not stop supervision
                traceback.print_exc(file=sys.stderr)

    def _contain(self, proc: Any) -> None:
        if self._job is None:
            self._job = _WinJob.create()
        if self._job is not None:
            self._job.add(proc)


def _log_lines(path: Path, offset: int) -> list[str]:
    """This run's last lines, read back from the file — for a child whose stderr was empty.

    That happens when the child's interpreter had no stdout or stderr at all
    and wrote to ``JARVIS_LOG`` directly instead of to our pipes.
    """
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(offset, size - _LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


class _WinJob:
    """A Windows job object that kills its processes when the app's handle closes.

    SILENT_BREAKAWAY: only the children added here are in it. What THEY start
    (a build runner, a browser) is not, so quitting Jarvis never takes a build
    or the user's browser with it.
    """

    _KILL_ON_JOB_CLOSE = 0x2000
    _SILENT_BREAKAWAY_OK = 0x1000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def create(cls) -> _WinJob | None:
        try:
            import ctypes
            from ctypes import wintypes

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _Io(ctypes.Structure):
                _fields_ = [
                    (n, ctypes.c_uint64)
                    for n in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")
                ]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Basic),
                    ("IoInfo", _Io),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.POINTER(_Extended),
                wintypes.DWORD,
            ]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = (
                cls._KILL_ON_JOB_CLOSE | cls._SILENT_BREAKAWAY_OK
            )
            if not k32.SetInformationJobObject(
                job, cls._EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ):
                return None
            return cls(k32, job)
        except Exception:  # noqa: BLE001 - containment is a safety net, not a requirement
            return None

    def add(self, proc: Any) -> None:
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return
        with suppress(Exception):
            self._kernel32.AssignProcessToJobObject(self._handle, int(handle))


# ───────────────────────────── what the app runs ─────────────────────────────


def app_specs() -> tuple[ProcessSpec, ...]:
    """The three children, always all three, so any of them can be (re)started later.

    Telegram is listed even with no token: a token saved in the HUD restarts it
    by name, and a supervisor that never knew the name could not. The database
    and config paths reach them through ``JARVIS_DB``/``JARVIS_CONFIG``, which
    every one of them already reads.
    """
    return (
        ProcessSpec("desk", ("-m", "jarvis", "desk")),
        ProcessSpec("schedule", ("-m", "jarvis.schedule")),
        ProcessSpec("telegram", ("-m", "jarvis.telegram")),
    )


def boot_names(*, telegram_token: bool, start_telegram: bool) -> tuple[str, ...]:
    """Which children start with the app.

    Telegram only with a token: without one it would exit at once, and the HUD
    would show a failure for a feature the user never turned on.
    """
    return ("desk", "schedule", *(("telegram",) if telegram_token and start_telegram else ()))


def exit_recorder(
    open_db: Callable[[], sqlite3.Connection], *, redactor: bus.Redactor | None = None
) -> Callable[[str, int, str], None]:
    """``on_exit`` for the app: one bus event, and the goodbye a killed child never wrote.

    A terminated process cannot run its own ``liveness.gone``, so without this
    the HUD would show a dead desk as listening for ten more seconds.
    """

    def record(name: str, code: int, reason: str) -> None:
        con = open_db()
        try:
            if name in liveness.PROCESSES:
                liveness.gone(con, name)
            bus.publish(
                con,
                "app.process_exited",
                "app",
                {"process": name, "code": code, "reason": reason},
                redactor=redactor,
            )
        finally:
            con.close()

    return record


def poll_forever(supervisor: Supervisor, stop: threading.Event, *, every: float = 1.0) -> None:
    """The supervision loop, for a thread of its own. Ends when ``stop`` is set."""
    while not stop.wait(every):
        try:
            supervisor.poll()
        except Exception:  # noqa: BLE001 - one bad tick must not end supervision
            traceback.print_exc(file=sys.stderr)


@dataclass
class Control:
    """What the window may ask of the app: its processes, a restart, and quitting.

    ``quit`` only sets the event. Teardown happens on the main thread that is
    waiting on it, after the HTTP response that asked for it has been sent.
    """

    supervisor: Supervisor
    quit_event: threading.Event

    def status(self) -> dict[str, dict[str, Any]]:
        return self.supervisor.status()

    def restart(self, name: str) -> None:
        if name not in self.supervisor.names:
            raise ValueError(f"There is no process called {name!r} to restart.")
        self.supervisor.restart(name)

    def quit(self) -> None:
        self.quit_event.set()
