"""Taking the picture on Windows: GDI through ctypes. No subprocess, no console, no file.

WHY NOT POWERSHELL ANY MORE. The backend this replaces ran PowerShell and
``System.Drawing``: from the windowed app that flashed a console window over the
very screen being photographed, wrote the picture to a temp file, and then took
seconds to decode an RGBA PNG in pure Python. Here the pixels go from the screen
DC to a bytes object in one process, in tens of milliseconds.

DPI, OR A THIRD OF THE SCREEN IS MISSING. Neither ``python.exe`` nor the
PyInstaller exe declares DPI awareness, so on a 150%-scaled display Windows
hands them LOGICAL metrics, and a copy sized from those gets the top-left two
thirds of the screen. Awareness is switched to per-monitor for THIS THREAD only
and switched back afterwards: tool calls run on pooled worker threads, and a
pool thread must be handed back the way it was found.

THE COMPOSED SCREEN, NOT THE WINDOW'S OWN DC. "This window" is the screen DC
cropped to the window's visible frame (``DWMWA_EXTENDED_FRAME_BOUNDS``; plain
``GetWindowRect`` includes an invisible resize border). A window's own DC, or
``PrintWindow``, commonly comes back black for GPU-drawn windows — browsers,
VS Code, Windows Terminal — which are most of what anyone asks about.

FAILURE IS A REFUSAL. A locked workstation, the UAC secure desktop, or a
window that excludes itself from capture comes back as one flat colour, or as
a failed BitBlt; both become :class:`~jarvis.capture.artifact.CaptureRefused`,
never a black picture sent to be described.

EVERY WIN32 CALL GOES THROUGH :class:`Win32`, built lazily, so the control flow
— clipping, scaling, DPI restore, the refusals — runs on Linux against a fake
with the same methods.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from jarvis.capture.artifact import CaptureRefused, Refusal, Subject
from jarvis.capture.backends import BackendStatus
from jarvis.capture.look import MAX_SIDE, Focus, fit
from jarvis.capture.png import RawImage, looks_blank
from jarvis.capture.policy import Window

__all__ = ["GdiCapturer", "Rect", "Win32", "bgra_to_rgb", "clip", "per_monitor_dpi"]

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
HALFTONE = 4
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: DPI_AWARENESS_CONTEXT pseudo-handles: per-monitor v2 (Windows 10 1703), then v1.
DPI_PER_MONITOR_V2 = -4
DPI_PER_MONITOR = -3


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def meets(self, other: Rect) -> bool:
        return clip(self, other).width > 0 and clip(self, other).height > 0


def clip(r: Rect, bounds: Rect) -> Rect:
    return Rect(
        max(r.left, bounds.left),
        max(r.top, bounds.top),
        min(r.right, bounds.right),
        min(r.bottom, bounds.bottom),
    )


def bgra_to_rgb(buf: bytes, width: int, height: int) -> RawImage:
    """A 32-bit top-down DIB (B, G, R, unused per pixel; no row padding) as RGB.

    Three slice assignments rather than a loop: a 2560x1440 frame is 3.7 million
    pixels, and this is the difference between 60 ms and several seconds.
    """
    if len(buf) != width * height * 4:
        raise ValueError(f"expected {width * height * 4} bytes, got {len(buf)}")
    rgb = bytearray(width * height * 3)
    rgb[0::3] = buf[2::4]
    rgb[1::3] = buf[1::4]
    rgb[2::3] = buf[0::4]
    return RawImage(width, height, bytes(rgb))


# ───────────────────────────── the real API ─────────────────────────────


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("right", ctypes.c_int32),
        ("bottom", ctypes.c_int32),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    # Room for the three colour masks GetDIBits may write; none are for 32-bit BI_RGB.
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def _rect(r: _RECT) -> Rect:
    return Rect(int(r.left), int(r.top), int(r.right), int(r.bottom))


class Win32:
    """The handful of user32/gdi32/dwmapi/kernel32 calls the capturer makes, prototyped.

    Fresh ``WinDLL`` handles with argtypes and restypes set here, never the
    shared ``ctypes.windll`` ones whose prototypes any other module may have
    changed (the pattern in :mod:`jarvis.jobs`).
    """

    def __init__(self) -> None:
        dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
        u = dll("user32", use_last_error=True)
        g = dll("gdi32", use_last_error=True)
        d = dll("dwmapi")
        k = dll("kernel32", use_last_error=True)
        # Win32's own names for the C types, so each prototype reads like its
        # documentation: HANDLE-ish pointers, int, DWORD, BOOL, LPWSTR.
        ptr, i32, dword, boolean = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_int
        wstr = ctypes.c_wchar_p

        def proto(fn: Any, args: tuple[Any, ...], res: Any) -> Any:
            fn.argtypes, fn.restype = args, res
            return fn

        try:
            self.set_thread_dpi: Any = proto(u.SetThreadDpiAwarenessContext, (ptr,), ptr)
        except AttributeError:  # older than Windows 10 1607: metrics stay as they are
            self.set_thread_dpi = None
        self._metrics = proto(u.GetSystemMetrics, (i32,), i32)
        self._get_dc = proto(u.GetDC, (ptr,), ptr)
        self._release_dc = proto(u.ReleaseDC, (ptr, ptr), i32)
        self._foreground = proto(u.GetForegroundWindow, (), ptr)
        self._title_len = proto(u.GetWindowTextLengthW, (ptr,), i32)
        self._title = proto(u.GetWindowTextW, (ptr, wstr, i32), i32)
        self._iconic = proto(u.IsIconic, (ptr,), boolean)
        self._visible = proto(u.IsWindowVisible, (ptr,), boolean)
        self._window_rect = proto(u.GetWindowRect, (ptr, ctypes.POINTER(_RECT)), boolean)
        self._thread_pid = proto(u.GetWindowThreadProcessId, (ptr, ctypes.POINTER(dword)), dword)
        self._monitor = proto(u.MonitorFromWindow, (ptr, dword), ptr)
        self._monitor_info = proto(u.GetMonitorInfoW, (ptr, ctypes.POINTER(_MONITORINFO)), boolean)
        self._enum_proc = ctypes.WINFUNCTYPE(boolean, ptr, ctypes.c_ssize_t)  # type: ignore[attr-defined]
        self._enum = proto(u.EnumWindows, (self._enum_proc, ctypes.c_ssize_t), boolean)
        self._mem_dc = proto(g.CreateCompatibleDC, (ptr,), ptr)
        self._bitmap = proto(g.CreateCompatibleBitmap, (ptr, i32, i32), ptr)
        self._select = proto(g.SelectObject, (ptr, ptr), ptr)
        self._bitblt = proto(g.BitBlt, (ptr, i32, i32, i32, i32, ptr, i32, i32, dword), boolean)
        self._stretch = proto(
            g.StretchBlt, (ptr, i32, i32, i32, i32, ptr, i32, i32, i32, i32, dword), boolean
        )
        self._stretch_mode = proto(g.SetStretchBltMode, (ptr, i32), i32)
        self._brush_org = proto(g.SetBrushOrgEx, (ptr, i32, i32, ptr), boolean)
        self._dibits = proto(
            g.GetDIBits,
            (
                ptr,
                ptr,
                ctypes.c_uint,
                ctypes.c_uint,
                ptr,
                ctypes.POINTER(_BITMAPINFO),
                ctypes.c_uint,
            ),
            i32,
        )
        self._delete = proto(g.DeleteObject, (ptr,), boolean)
        self._delete_dc = proto(g.DeleteDC, (ptr,), boolean)
        self._dwm = proto(d.DwmGetWindowAttribute, (ptr, dword, ptr, dword), ctypes.c_long)
        self._open = proto(k.OpenProcess, (dword, boolean, dword), ptr)
        self._image = proto(
            k.QueryFullProcessImageNameW, (ptr, dword, wstr, ctypes.POINTER(dword)), boolean
        )
        self._close = proto(k.CloseHandle, (ptr,), boolean)

    def virtual_screen(self) -> Rect:
        """Every monitor's union, in the thread's current DPI mode. x may be negative."""
        m = self._metrics
        x, y = m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN)
        return Rect(x, y, x + m(SM_CXVIRTUALSCREEN), y + m(SM_CYVIRTUALSCREEN))

    def foreground(self) -> int:
        return int(self._foreground() or 0)

    def window_title(self, hwnd: int) -> str:
        n = self._title_len(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self._title(hwnd, buf, n + 1)
        return buf.value

    def window_exe(self, hwnd: int) -> str:
        pid = ctypes.c_uint32()
        self._thread_pid(hwnd, ctypes.byref(pid))
        handle = self._open(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
        if not handle:
            return ""  # an elevated process; the title is still checked
        try:
            size = ctypes.c_uint32(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = self._image(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            self._close(handle)

    def minimised(self, hwnd: int) -> bool:
        return bool(self._iconic(hwnd))

    def frame(self, hwnd: int) -> Rect:
        """The VISIBLE frame; GetWindowRect only when DWM will not say."""
        r = _RECT()
        hr = self._dwm(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
        if hr != 0 and not self._window_rect(hwnd, ctypes.byref(r)):
            raise OSError("no rectangle for that window")
        return _rect(r)

    def monitor_of(self, hwnd: int) -> Rect:
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not self._monitor_info(
            self._monitor(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info)
        ):
            raise OSError("GetMonitorInfoW failed")
        return _rect(info.rcMonitor)

    def visible_windows(self) -> list[tuple[int, Window, Rect]]:
        """Top-level windows a picture would show: visible, not minimised, not cloaked.

        Cloaked windows are the ones on other virtual desktops and suspended
        Store apps: "visible" to the API, absent from the screen.
        """
        found: list[int] = []

        def collect(hwnd: Any, _: Any) -> int:
            found.append(int(hwnd or 0))
            return 1

        self._enum(self._enum_proc(collect), 0)
        out: list[tuple[int, Window, Rect]] = []
        for hwnd in found:
            if not hwnd or not self._visible(hwnd) or self._iconic(hwnd):
                continue
            cloaked = ctypes.c_uint32(0)
            self._dwm(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                continue
            try:
                rect = self.frame(hwnd)
            except OSError:
                continue
            if rect.width and rect.height:
                window = Window(title=self.window_title(hwnd), app=self.window_exe(hwnd))
                out.append((hwnd, window, rect))
        return out

    def copy(self, src: Rect, out_w: int, out_h: int) -> bytes:
        """BitBlt — or a HALFTONE StretchBlt when scaling — into a 32-bit top-down DIB."""
        screen = self._get_dc(None)
        if not screen:
            raise OSError("GetDC(NULL) failed: no interactive desktop")
        mem = bmp = old = None
        try:
            mem = self._mem_dc(screen)
            bmp = self._bitmap(screen, out_w, out_h)
            if not mem or not bmp:
                raise OSError("could not allocate a bitmap that size")
            old = self._select(mem, bmp)
            if (out_w, out_h) == (src.width, src.height):
                ok = self._bitblt(mem, 0, 0, out_w, out_h, screen, src.left, src.top, SRCCOPY)
            else:
                self._stretch_mode(mem, HALFTONE)
                self._brush_org(mem, 0, 0, None)  # required after choosing HALFTONE
                ok = self._stretch(
                    mem, 0, 0, out_w, out_h, screen, src.left, src.top, src.width, src.height,
                    SRCCOPY,
                )  # fmt: skip
            if not ok:
                raise OSError(f"BitBlt failed (error {ctypes.get_last_error()})")  # type: ignore[attr-defined]
            # The bitmap must not be selected into a DC while GetDIBits reads it.
            self._select(mem, old)
            old = None
            info = _BITMAPINFO()
            hdr = info.bmiHeader
            hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            hdr.biWidth, hdr.biHeight = out_w, -out_h  # negative: rows top-down, no flip
            hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, BI_RGB
            buf = ctypes.create_string_buffer(out_w * out_h * 4)
            lines = self._dibits(mem, bmp, 0, out_h, buf, ctypes.byref(info), DIB_RGB_COLORS)
            if lines != out_h:
                raise OSError(f"GetDIBits returned {lines} of {out_h} rows")
            return buf.raw
        finally:
            if old is not None:
                self._select(mem, old)
            if bmp:
                self._delete(bmp)
            if mem:
                self._delete_dc(mem)
            self._release_dc(None, screen)


@contextmanager
def per_monitor_dpi(api: Any) -> Iterator[None]:
    """Physical pixels for THIS thread, and the thread handed back as it was found."""
    fn = getattr(api, "set_thread_dpi", None)
    previous = None
    if fn is not None:
        previous = fn(ctypes.c_void_p(DPI_PER_MONITOR_V2)) or fn(ctypes.c_void_p(DPI_PER_MONITOR))
    try:
        yield
    finally:
        if fn is not None and previous:
            # Handed back exactly as received; the prototype's argtypes turn
            # the integer handle into a pointer.
            fn(previous)


class GdiCapturer:
    """A :class:`~jarvis.capture.backends.Capturer` and a ``look.Focuser``, for Windows."""

    def __init__(self, api: Any | None = None, *, max_side: int = MAX_SIDE) -> None:
        # Built on first use: constructing one must not touch Win32, so
        # choose_capturer can be called (and tested) on any platform.
        self._api = api
        self.max_side = max_side

    @property
    def name(self) -> str:
        return "gdi"

    def status(self) -> BackendStatus:
        return BackendStatus(self.name, available=True, note="ctypes GDI; no subprocess")

    def _win(self) -> Any:
        if self._api is None:
            self._api = Win32()
        return self._api

    def focus(self, subject: Subject) -> Focus:
        """The window in front — title and exe, for the never-capture list — and, for
        ``screen``, every other window visible on its monitor."""
        api = self._win()
        with per_monitor_dpi(api):
            hwnd = api.foreground()
            if not hwnd:
                return Focus()
            window = Window(title=api.window_title(hwnd), app=api.window_exe(hwnd))
            beside: tuple[Window, ...] = ()
            if subject == "screen":
                monitor = api.monitor_of(hwnd)
                beside = tuple(
                    w for h, w, r in api.visible_windows() if h != hwnd and r.meets(monitor)
                )
            return Focus(window=window, window_id=str(hwnd), beside=beside)

    def grab(self, subject: Subject, *, window_id: str | None = None) -> RawImage:
        api = self._win()
        try:
            with per_monitor_dpi(api):
                bounds = api.virtual_screen()
                hwnd = int(window_id) if window_id else api.foreground()
                if subject == "pane":
                    if not hwnd or api.minimised(hwnd):
                        raise CaptureRefused(
                            Refusal("capture_failed", "no visible window in front")
                        )
                    src = clip(api.frame(hwnd), bounds)
                elif subject == "screen":
                    src = clip(api.monitor_of(hwnd), bounds) if hwnd else bounds
                else:
                    raise CaptureRefused(
                        Refusal("subject_not_allowed", f"GDI cannot capture {subject!r}")
                    )
                if src.width == 0 or src.height == 0:
                    raise CaptureRefused(Refusal("capture_failed", "that window is off screen"))
                w, h = fit(src.width, src.height, self.max_side)
                raw = api.copy(src, w, h)
                if window_id and api.foreground() != hwnd:
                    # The policy checked one window; another is in front now and
                    # may be what the screen DC just copied.
                    raise CaptureRefused(
                        Refusal("capture_failed", "the window in front changed while I looked")
                    )
        except CaptureRefused:
            raise
        except (OSError, ValueError) as exc:
            raise CaptureRefused(
                Refusal("capture_failed", f"gdi: {exc}", "is the screen locked?")
            ) from exc
        img = bgra_to_rgb(raw, w, h)
        if looks_blank(img):
            raise CaptureRefused(
                Refusal("capture_failed", "the capture is one flat colour", "unlock the screen")
            )
        return img
