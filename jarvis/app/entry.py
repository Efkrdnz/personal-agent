"""The one entry point of the double-clickable app: the frozen exe and the pip launcher.

ONE EXE, EVERY PROCESS. Frozen, ``sys.executable`` is ``Jarvis.exe`` itself, so
every existing ``[sys.executable, "-m", "jarvis...", ...]`` spawn — the
supervisor's children, the Claude Code runner — starts Jarvis.exe again with
``-m`` in front. This module answers that from an EXPLICIT table rather than
``runpy``: a frozen build has no ``-m`` of its own, and a table names exactly
which modules may be run, so a stray argument cannot run anything else.

NO CONSOLE, SO NO STREAMS. A windowed interpreter (pythonw, or an exe built
with ``console=False``) starts with ``sys.stdout`` and ``sys.stderr`` set to
None, and the first ``print`` is an AttributeError that kills the app with no
trace anywhere. They are pointed at a log file before anything else runs.
"""

from __future__ import annotations

import faulthandler
import multiprocessing
import os
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import IO

from jarvis.app import paths

__all__ = ["CHILDREN", "main", "redirect_missing_streams", "run_module"]


# Each import is inside its function: the scheduler should not pay for loading
# the desk, and a frozen build's analyser still sees every one of them.
def _jarvis(args: list[str]) -> int:
    from jarvis.__main__ import main as run

    return run(args)


def _schedule(args: list[str]) -> int:
    from jarvis.schedule.__main__ import main as run

    return run(args)


def _telegram(args: list[str]) -> int:
    from jarvis.telegram.__main__ import main as run

    return run(args)


def _cc(args: list[str]) -> int:
    from jarvis.cc.__main__ import main as run

    return run(args)


#: ``-m <module>`` -> its ``main(argv)``. Nothing outside this table can be run.
CHILDREN: Mapping[str, Callable[[list[str]], int]] = {
    "jarvis": _jarvis,
    "jarvis.schedule": _schedule,
    "jarvis.telegram": _telegram,
    "jarvis.cc": _cc,
}


def redirect_missing_streams(*, env: Mapping[str, str] | None = None) -> IO[str] | None:
    """Give a windowed process somewhere to write. Returns the log stream, or None if not needed.

    ``JARVIS_LOG`` first: the supervisor sets it to the child's own log, so a
    child that was handed no usable stdout still writes where the HUD points.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    env = os.environ if env is None else env
    target = env.get("JARVIS_LOG") or str(paths.log_path("app"))
    # Line-buffered: a crash must not take the last lines with it.
    stream = open(target, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    # A hard crash in native code (PortAudio, onnxruntime) prints nothing
    # through Python; faulthandler at least leaves the stack in the log.
    with suppress(Exception):
        faulthandler.enable(stream)
    return stream


def run_module(args: Sequence[str]) -> int:
    """``-m <module> [args...]``, from the table."""
    if not args:
        print("-m needs a module name", file=sys.stderr)
        return 2
    module, rest = args[0], list(args[1:])
    fn = CHILDREN.get(module)
    if fn is None:
        print(
            f"Jarvis cannot run -m {module}; it runs only {', '.join(sorted(CHILDREN))}",
            file=sys.stderr,
        )
        return 2
    return int(fn(rest) or 0)


def main(
    argv: Sequence[str] | None = None,
    *,
    alert: Callable[[str, str], None] | None = None,
) -> int:
    """Run a child (``-m ...``) or, with anything else, the app."""
    # First: in a frozen build a multiprocessing child re-enters here, and
    # this is what sends it to its target instead of starting a second app.
    multiprocessing.freeze_support()
    redirect_missing_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["-m"]:
        return run_module(args[1:])
    try:
        return _jarvis(["app", *args])
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - the last place anybody can be told
        traceback.print_exc()
        # Never during --selftest: a dialog on a CI runner waits for a click
        # that never comes, and the job times out instead of failing.
        show = alert if alert is not None else _default_alert()
        if show is not None and "--selftest" not in args:
            where = getattr(sys.stderr, "name", "") or "the log"
            with suppress(Exception):
                show("Jarvis", f"Jarvis couldn't start: {exc}\n\nThe details are in {where}.")
        return 1


def _default_alert() -> Callable[[str, str], None] | None:
    """A Windows message box, or None elsewhere (where stderr is a terminal anyway).

    A double-clicked app that dies at startup otherwise vanishes without a
    word: no console, and a log file nobody knows exists.
    """
    if sys.platform != "win32":
        return None

    def box(title: str, text: str) -> None:
        import ctypes

        # MB_ICONERROR | MB_SETFOREGROUND: on top, so it is not hidden behind
        # whatever the user was looking at when they double-clicked.
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 | 0x10000)  # type: ignore[attr-defined]

    return box
