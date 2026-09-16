"""Taking the picture, per platform, with the failure modes named in advance.

Every one of these has a way of failing that looks like success, and each one is
handled here rather than discovered by a user who got an empty grey rectangle.

X11 — the easy one. ``maim``, ``import``, ``scrot`` or ``xwd`` will photograph
the root window or a named window id with no prompt, no daemon and no session
bus. Nothing here depends on which one is installed: the tools are a table of
argv templates and the first one on PATH wins.

WAYLAND — THE ONE THAT BREAKS THE USE CASE, so it is stated plainly. There is no
unattended screenshot on a standard Wayland desktop. A client cannot read the
screen; it must ask ``org.freedesktop.portal.Desktop``'s ``Screenshot`` interface,
and the portal shows a CONSENT DIALOG on the machine being photographed. GNOME's
implementation always does; others may honour ``interactive=false`` and still
prompt once. That dialog is fatal for "Jarvis, send me a screenshot" *while the
user is out*, which is the entire reason this package exists — nobody is there to
click it, and the D-Bus call simply does not return. So Wayland is detected and
REFUSED with :data:`~jarvis.capture.artifact.RefusalCode` ``wayland_consent``
rather than attempted, because a refusal that arrives in two seconds is worth
more than a capture that arrives never.

CAN A PORTAL TOKEN PERSIST THAT CONSENT? Not for screenshots. The persistence
mechanism (``persist_mode`` plus a ``restore_token`` handed back on the next
call) belongs to the ``ScreenCast`` interface, not to ``Screenshot`` — it exists
so a video conference can keep sharing a window across restarts. So the three
honest ways to get an unattended picture on Wayland are: (1) hold a ScreenCast
session that the user approved *while present*, with a restore token, and pull
frames from it — a real design, a real amount of work, and a pipewire dependency
this package does not have; (2) a compositor-specific tool that bypasses the
portal, which on wlroots desktops (Sway, Hyprland, river) is ``grim`` and works
with no prompt at all — supported here, and detected before the portal is even
considered; (3) accept the dialog and only screenshot while the user is at the
desk, which is not the use case. Anyone changing this should re-check against
the portal's own docs first: this is version-dependent behaviour, and it is the
kind of fact :file:`docs/findings.md` exists to hold.

macOS — Screen Recording permission, granted once in System Settings ▸ Privacy &
Security, prompted for only the FIRST time. The trap is what happens when it has
not been granted: ``screencapture`` exits 0 and writes a valid PNG of the desktop
wallpaper with every window missing. There is no error to check. So permission is
inferred after the fact, from the picture: a capture that is one uniform colour
is treated as un-granted and refused with the remedy attached. That is a
heuristic and it is labelled as one; the real API (``CGPreflightScreenCaptureAccess``)
is not reachable from the standard library, and adding a compiled dependency to
ask a yes/no question is not a trade this package makes.

Windows — generally fine. PowerShell plus ``System.Drawing`` copies the virtual
screen into a bitmap and saves PNG, with no permission model in the way.

EVERY REAL BACKEND IS UNTESTABLE HERE, and that is a design input rather than an
excuse. This machine has no display at all, and CI never will. So the platform
work is a declarative table plus one small class that runs argv and decodes the
result, the *choice* of backend is a pure function of the environment, and the
two capturers the tests actually exercise — :class:`NullCapturer` and
:class:`SyntheticCapturer` — implement the same protocol as the real ones.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from jarvis.capture.artifact import CaptureRefused, Refusal, Subject
from jarvis.capture.png import Canvas, RawImage, decode_png, looks_blank

__all__ = [
    "CAPTURE_TOOLS",
    "WAYLAND_REFUSAL",
    "BackendStatus",
    "CommandCapturer",
    "Capturer",
    "NullCapturer",
    "SyntheticCapturer",
    "Tool",
    "choose_capturer",
    "detect_backend",
]

#: How long a screenshot tool gets. A capture that has not produced bytes in ten
#: seconds is either blocked on a dialog or wedged, and both call for the typed
#: refusal rather than a longer wait.
CAPTURE_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class Tool:
    """One external screenshot program, as argv templates.

    ``{out}`` is the PNG path and ``{window}`` is a platform window id. A tool
    with no ``window`` template simply cannot capture a single pane, and says so
    by omission rather than by silently photographing the whole screen instead.
    """

    name: str
    screen: tuple[str, ...]
    window: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("linux",)
    session: str = "x11"

    def argv(self, subject: Subject, out: Path, window_id: str | None) -> tuple[str, ...]:
        if subject == "pane":
            if not self.window or not window_id:
                raise ValueError(f"{self.name} cannot capture a single window")
            template = self.window
        else:
            template = self.screen
        return tuple(part.format(out=str(out), window=window_id or "") for part in template)


#: Ordered by preference within each session type: the first one on PATH wins.
#: ``grim`` is first on Wayland precisely because it is the one path that does
#: not go through the consent dialog.
CAPTURE_TOOLS: tuple[Tool, ...] = (
    Tool("grim", ("grim", "{out}"), session="wayland"),
    Tool(
        "maim",
        ("maim", "--hidecursor", "--format=png", "{out}"),
        ("maim", "-i", "{window}", "--format=png", "{out}"),
    ),
    Tool(
        "import",
        ("import", "-window", "root", "png:{out}"),
        ("import", "-window", "{window}", "png:{out}"),
    ),
    Tool(
        "scrot",
        ("scrot", "--overwrite", "{out}"),
        ("scrot", "--overwrite", "--window", "{window}", "{out}"),
    ),
    Tool(
        "screencapture",
        ("screencapture", "-x", "-t", "png", "{out}"),
        ("screencapture", "-x", "-t", "png", "-l", "{window}", "{out}"),
        platforms=("darwin",),
        session="quartz",
    ),
    Tool(
        "powershell",
        (
            "powershell",
            "-NoProfile",
            "-Command",
            "Add-Type -AssemblyName System.Drawing,System.Windows.Forms; "
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($i); "
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
            "$i.Save('{out}',[System.Drawing.Imaging.ImageFormat]::Png)",
        ),
        platforms=("win32",),
        session="gdi",
    ),
)

WAYLAND_REFUSAL = Refusal(
    code="wayland_consent",
    detail=(
        "this is a Wayland session with no compositor-native screenshot tool, so a capture "
        "would have to go through xdg-desktop-portal, which shows a consent dialog on the "
        "machine being photographed"
    ),
    remedy=(
        "install grim on a wlroots compositor, log into the X11 session, or ask for the "
        "transcript instead — it is the better answer anyway"
    ),
)


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """Whether a picture is possible here, and if not, why and what to do.

    Carries the refusal rather than a bare bool so that "no screenshots on this
    box" is one object that can be logged, spoken and tested, instead of a
    condition reconstructed at three call sites.
    """

    name: str
    available: bool
    refusal: Refusal | None = None
    needs_consent_dialog: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not self.available and self.refusal is None:
            raise ValueError("an unavailable backend must say why")


@runtime_checkable
class Capturer(Protocol):
    """Take a picture, or refuse. Nothing here knows about redaction or delivery."""

    @property
    def name(self) -> str: ...

    def status(self) -> BackendStatus: ...

    def grab(self, subject: Subject, *, window_id: str | None = None) -> RawImage:
        """Return pixels, or raise :class:`CaptureRefused`."""


class NullCapturer:
    """Always refuses. The correct backend for a headless box, and the default.

    Not a stub: this is what runs on a server, in CI, and on the machine this
    package was written on, and "there is no screen here" is a real answer that
    a user can act on.
    """

    def __init__(self, refusal: Refusal | None = None) -> None:
        self._refusal = refusal or Refusal(
            code="no_display",
            detail="no display server is visible from this process",
            remedy="ask for the transcript, which needs no display at all",
        )

    @property
    def name(self) -> str:
        return "null"

    def status(self) -> BackendStatus:
        return BackendStatus(self.name, available=False, refusal=self._refusal)

    def grab(self, subject: Subject, *, window_id: str | None = None) -> RawImage:
        raise CaptureRefused(self._refusal)


class SyntheticCapturer:
    """A deterministic fake screen, so the rest of this package is testable.

    Draws the supplied lines in the 5x7 font on a plain background, at a known
    position, which means a test can say "the secret is at row 3" and then assert
    that row 3 is solid magenta afterwards. Same bytes every run: no clock, no
    randomness, no environment.
    """

    def __init__(
        self,
        *,
        width: int = 320,
        height: int = 200,
        lines: Sequence[str] = (),
        background: tuple[int, int, int] = (16, 16, 16),
        foreground: tuple[int, int, int] = (210, 210, 210),
        scale: int = 2,
        blank: bool = False,
    ) -> None:
        self._width = width
        self._height = height
        self._lines = tuple(lines)
        self._bg = background
        self._fg = foreground
        self._scale = scale
        # Reproduces the macOS no-permission result: a successful capture of
        # nothing at all. Exists so the heuristic that detects it has something
        # to detect.
        self._blank = blank

    @property
    def name(self) -> str:
        return "synthetic"

    @property
    def line_height(self) -> int:
        return 9 * self._scale

    def line_top(self, index: int) -> int:
        return 4 + index * self.line_height

    def status(self) -> BackendStatus:
        return BackendStatus(self.name, available=True, note="synthetic pixels, not a real screen")

    def grab(self, subject: Subject, *, window_id: str | None = None) -> RawImage:
        width = self._width if subject == "screen" else self._width * 3 // 4
        canvas = Canvas.blank(width, self._height, self._bg)
        if not self._blank:
            for i, line in enumerate(self._lines):
                canvas.text(4, self.line_top(i), line, self._fg, scale=self._scale)
        return canvas.freeze()


class CommandCapturer:
    """Drive an external screenshot program and decode what it wrote.

    The subprocess runner is injected so the argv this builds can be asserted
    without a display, which is the only part of the real path that can be
    checked on this machine — and the part most likely to be wrong.
    """

    def __init__(
        self,
        tool: Tool,
        *,
        status: BackendStatus | None = None,
        run: Callable[[Sequence[str], Path], None] | None = None,
        blank_means_no_permission: bool = False,
    ) -> None:
        self._tool = tool
        self._status = status or BackendStatus(tool.name, available=True)
        self._run = run or _run_capture
        self._blank_means_no_permission = blank_means_no_permission

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def tool(self) -> Tool:
        return self._tool

    def status(self) -> BackendStatus:
        return self._status

    def grab(self, subject: Subject, *, window_id: str | None = None) -> RawImage:
        if not self._status.available and self._status.refusal is not None:
            raise CaptureRefused(self._status.refusal)

        with tempfile.TemporaryDirectory(prefix="jarvis-capture-") as tmp:
            out = Path(tmp) / "shot.png"
            try:
                self._run(self._tool.argv(subject, out, window_id), out)
                data = out.read_bytes()
            except CaptureRefused:
                raise
            except Exception as exc:
                raise CaptureRefused(
                    Refusal(
                        code="capture_failed",
                        detail=f"{self._tool.name} failed: {exc}",
                        remedy="ask for the whole screen, or for the transcript",
                    )
                ) from exc

        try:
            img = decode_png(data)
        except (ValueError, struct.error) as exc:
            # struct.error is not a ValueError. `decode_png` is written not to
            # let one out, but this is the boundary where a decoder bug has to
            # become a refusal rather than an untyped crash on the caller.
            raise CaptureRefused(
                Refusal(
                    code="undecodable",
                    detail=f"{self._tool.name} produced a PNG this decoder cannot read: {exc}",
                    remedy=(
                        "a picture that cannot be decoded cannot be redacted, so nothing is "
                        "sent; ask for the transcript"
                    ),
                )
            ) from exc

        if self._blank_means_no_permission and looks_blank(img):
            raise CaptureRefused(_MACOS_PERMISSION)
        return img


_MACOS_PERMISSION = Refusal(
    code="macos_permission",
    detail=(
        "the capture succeeded but every pixel is the same colour, which is what macOS "
        "returns when Screen Recording permission has not been granted"
    ),
    remedy=(
        "System Settings > Privacy & Security > Screen Recording, tick the app running "
        "Jarvis, then restart it — the prompt only ever appears once"
    ),
)


def _run_capture(argv: Sequence[str], out: Path) -> None:
    # argv is built from CAPTURE_TOOLS and a window id, never from user text, and
    # there is no shell: a title containing a semicolon is an argument, not a command.
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        timeout=CAPTURE_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("wrote no image")


def detect_backend(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> BackendStatus:
    """Which capture backend this machine has, as a pure function of its inputs.

    Everything it looks at is an argument with a real default, so the Wayland
    verdict can be tested on a Linux box with no display and the macOS verdict
    can be tested on that same box.
    """
    plat = platform or _platform()
    environ = env if env is not None else os.environ
    look = which or shutil.which

    if plat == "darwin":
        if look("screencapture") is None:
            return BackendStatus(
                "screencapture",
                available=False,
                refusal=Refusal(
                    code="no_backend",
                    detail="screencapture is not on PATH, which should be impossible on macOS",
                    remedy="check PATH",
                ),
            )
        return BackendStatus(
            "screencapture",
            available=True,
            note=(
                "Screen Recording permission cannot be checked before the fact from the "
                "standard library; an all-one-colour result is treated as un-granted"
            ),
        )

    if plat == "win32":
        if look("powershell") is None:
            return BackendStatus(
                "powershell",
                available=False,
                refusal=Refusal(
                    code="no_backend",
                    detail="powershell is not on PATH",
                    remedy="check PATH",
                ),
            )
        return BackendStatus("powershell", available=True)

    wayland = bool(environ.get("WAYLAND_DISPLAY")) or environ.get("XDG_SESSION_TYPE") == "wayland"
    if wayland:
        if look("grim") is not None:
            return BackendStatus(
                "grim",
                available=True,
                note="wlroots compositor: grim bypasses the portal, so there is no consent dialog",
            )
        return BackendStatus(
            "portal",
            available=False,
            refusal=WAYLAND_REFUSAL,
            needs_consent_dialog=True,
        )

    if not environ.get("DISPLAY"):
        return BackendStatus(
            "null",
            available=False,
            refusal=Refusal(
                code="no_display",
                detail="neither DISPLAY nor WAYLAND_DISPLAY is set",
                remedy="ask for the transcript, which needs no display at all",
            ),
        )

    for tool in CAPTURE_TOOLS:
        if tool.session == "x11" and plat in tool.platforms and look(tool.name) is not None:
            return BackendStatus(tool.name, available=True)

    return BackendStatus(
        "null",
        available=False,
        refusal=Refusal(
            code="no_backend",
            detail="an X11 display is present but none of "
            + ", ".join(t.name for t in CAPTURE_TOOLS if t.session == "x11")
            + " is installed",
            remedy="install maim or imagemagick, or ask for the transcript",
        ),
    )


def choose_capturer(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> Capturer:
    """The best real capturer for this machine, or a :class:`NullCapturer`.

    Returning a refusing capturer rather than ``None`` means the caller has one
    code path: it always has a :class:`Capturer`, and the refusal arrives from
    :meth:`Capturer.grab` with a sentence attached.
    """
    status = detect_backend(platform=platform, env=env, which=which)
    if not status.available:
        return NullCapturer(status.refusal)
    tool = next((t for t in CAPTURE_TOOLS if t.name == status.name), None)
    if tool is None:  # pragma: no cover - only reachable if the table and detect disagree
        return NullCapturer(
            Refusal(
                code="no_backend",
                detail=f"detect_backend chose {status.name!r}, which is not in CAPTURE_TOOLS",
                remedy="fix the table",
            )
        )
    return CommandCapturer(
        tool,
        status=status,
        blank_means_no_permission=(tool.session == "quartz"),
    )


def _platform() -> str:
    return sys.platform
