"""The Windows capturer: GDI through ctypes, its control flow tested here against a fake.

The fake has the same methods as :class:`jarvis.capture.windows.Win32`, so the
decisions — which rectangle, how big, DPI handed back, what is refused — run
on Linux. The Windows-only tests at the bottom drive the real API on the
windows-latest CI job, without ever asserting what is on that screen.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.capture import (
    CAPTURE_TOOLS,
    Capturer,
    CaptureRefused,
    Focuser,
    GdiCapturer,
    choose_capturer,
    decode_png,
    detect_backend,
    encode_png,
)
from jarvis.capture import windows as gdi
from jarvis.capture.look import MAX_SIDE, fit
from jarvis.capture.policy import Window
from jarvis.capture.windows import Rect, bgra_to_rgb, clip

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="drives the real Win32 API")
ROOT = Path(__file__).resolve().parents[1]

CODE = r"C:\Users\u\AppData\Local\Programs\Microsoft VS Code\Code.exe"


class FakeWin32:
    def __init__(self, *, blank: bool = False, fail: bool = False, hwnd: int = 42) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.dpi: list[Any] = ["unaware"]
        self.blank = blank
        self.fail = fail
        self.hwnd = hwnd
        self.after_copy: int | None = None  # the window in front once the copy is done
        self.others: list[tuple[int, Window, Rect]] = []

    def set_thread_dpi(self, ctx: Any) -> Any:
        prev = self.dpi[-1]
        self.dpi.append(ctx)
        return prev

    def virtual_screen(self) -> Rect:
        return Rect(-1920, 0, 2560, 1440)  # a second monitor to the left, at negative x

    def foreground(self) -> int:
        return self.hwnd

    def window_title(self, hwnd: int) -> str:
        return {42: "main.py - Visual Studio Code"}.get(hwnd, "")

    def window_exe(self, hwnd: int) -> str:
        return CODE if hwnd == 42 else ""

    def minimised(self, hwnd: int) -> bool:
        return hwnd == 7

    def frame(self, hwnd: int) -> Rect:
        return Rect(2000, 100, 2700, 600)  # hangs off the right edge

    def monitor_of(self, hwnd: int) -> Rect:
        return Rect(0, 0, 2560, 1440)

    def visible_windows(self) -> list[tuple[int, Window, Rect]]:
        return [(self.hwnd, Window("main.py - Visual Studio Code", CODE), Rect(0, 0, 9, 9))] + list(
            self.others
        )

    def copy(self, src: Rect, w: int, h: int) -> bytes:
        self.calls.append(("copy", src, w, h))
        if self.after_copy is not None:
            self.hwnd = self.after_copy
        if self.fail:
            raise OSError("BitBlt failed (error 6)")
        if self.blank:
            return bytes(w * h * 4)
        out = bytearray(w * h * 4)
        out[0:4] = bytes([10, 20, 30, 0])  # B G R X of pixel (0, 0)
        return bytes(out)


def test_it_is_a_capturer_and_can_say_what_is_in_front() -> None:
    cap = GdiCapturer(FakeWin32())
    assert isinstance(cap, Capturer) and isinstance(cap, Focuser)
    assert cap.status().available and cap.name == "gdi"


def test_constructing_one_touches_no_win32() -> None:
    # choose_capturer runs in every process, on every platform.
    assert GdiCapturer()._api is None


def test_the_window_is_its_visible_frame_clipped_to_the_screen() -> None:
    api = FakeWin32()
    img = GdiCapturer(api).grab("pane", window_id="42")
    assert api.calls == [("copy", Rect(2000, 100, 2560, 600), 560, 500)]
    assert img.pixel(0, 0) == (30, 20, 10)  # BGRX -> RGB
    assert decode_png(encode_png(img)) == img


def test_the_screen_is_the_monitor_the_window_is_on_scaled_to_the_cap() -> None:
    api = FakeWin32()
    GdiCapturer(api, max_side=1280).grab("screen", window_id="42")
    assert api.calls == [("copy", Rect(0, 0, 2560, 1440), 1280, 720)]


def test_dpi_is_per_monitor_while_copying_and_handed_back_after_a_failure() -> None:
    api = FakeWin32(fail=True)
    with pytest.raises(CaptureRefused) as e:
        GdiCapturer(api).grab("screen")
    assert e.value.refusal.code == "capture_failed"
    asked = api.dpi[1]
    assert isinstance(asked, ctypes.c_void_p)
    assert ctypes.c_void_p(gdi.DPI_PER_MONITOR_V2).value == asked.value
    assert api.dpi[-1] == "unaware"  # restored to what the pooled thread had


def test_a_flat_picture_is_refused_not_described() -> None:
    with pytest.raises(CaptureRefused, match="flat colour"):
        GdiCapturer(FakeWin32(blank=True)).grab("screen")


def test_a_minimised_window_cannot_be_looked_at() -> None:
    with pytest.raises(CaptureRefused):
        GdiCapturer(FakeWin32(hwnd=7)).grab("pane")


def test_if_another_window_comes_in_front_mid_copy_the_picture_is_refused() -> None:
    api = FakeWin32()
    api.after_copy = 99
    with pytest.raises(CaptureRefused, match="changed"):
        GdiCapturer(api).grab("pane", window_id="42")


def test_focus_names_the_window_and_for_the_screen_everything_beside_it() -> None:
    api = FakeWin32()
    keepass = (
        5,
        Window("Database - KeePassXC", r"C:\Program Files\KeePassXC\KeePassXC.exe"),
        Rect(10, 10, 500, 500),
    )
    elsewhere = (6, Window("Other monitor", "x.exe"), Rect(-1900, 0, -100, 900))
    api.others = [keepass, elsewhere]
    cap = GdiCapturer(api)
    window = cap.focus("pane")
    assert window.window_id == "42" and window.window.known and "Code.exe" in window.window.app
    assert window.beside == ()  # only the window is photographed
    screen = cap.focus("screen")
    assert screen.beside == (keepass[1],)  # on this monitor, and not the window itself
    assert api.dpi[-1] == "unaware"


def test_nothing_in_front_is_an_unknown_window() -> None:
    focus = GdiCapturer(FakeWin32(hwnd=0)).focus("pane")
    assert focus.window.known is False and focus.window_id is None


def test_helpers() -> None:
    assert fit(3840, 2160, MAX_SIDE) == (2560, 1440)
    assert fit(800, 600, MAX_SIDE) == (800, 600)
    assert clip(Rect(-5, -5, 10, 10), Rect(0, 0, 8, 8)) == Rect(0, 0, 8, 8)
    assert Rect(0, 0, 5, 5).meets(Rect(4, 4, 9, 9)) and not Rect(0, 0, 5, 5).meets(Rect(5, 0, 9, 9))
    with pytest.raises(ValueError):
        bgra_to_rgb(b"\x00" * 5, 1, 1)


def test_the_structures_have_the_sizes_win32_expects() -> None:
    assert ctypes.sizeof(gdi._BITMAPINFOHEADER) == 40
    assert ctypes.sizeof(gdi._RECT) == 16
    assert ctypes.sizeof(gdi._MONITORINFO) == 40


# ───────────────────────────── the backend choice ─────────────────────────────


def test_windows_gets_gdi_and_never_a_console_program() -> None:
    status = detect_backend(platform="win32", env={}, which=lambda name: None)
    assert status.name == "gdi" and status.available
    assert isinstance(choose_capturer(platform="win32", env={}), GdiCapturer)
    assert not any("win32" in tool.platforms for tool in CAPTURE_TOOLS)
    assert "powershell" not in {tool.name for tool in CAPTURE_TOOLS}


def test_the_remaining_command_backends_are_run_hidden_anyway() -> None:
    src = (ROOT / "jarvis/capture/backends.py").read_text(encoding="utf-8")
    assert "creationflags=0x08000000" in src


# ───────────────────────────── Windows, for real ─────────────────────────────


@windows_only
def test_windows_the_api_prototypes_load_and_report_a_screen() -> None:
    api = gdi.Win32()
    with gdi.per_monitor_dpi(api):
        screen = api.virtual_screen()
    assert screen.width > 0 and screen.height > 0
    assert isinstance(api.foreground(), int)
    assert all(isinstance(w, Window) for _, w, _ in api.visible_windows())


@windows_only
def test_windows_the_threads_dpi_awareness_is_handed_back() -> None:
    user32 = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.AreDpiAwarenessContextsEqual.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    before = user32.GetThreadDpiAwarenessContext()
    api = gdi.Win32()
    with gdi.per_monitor_dpi(api):
        pass
    after = user32.GetThreadDpiAwarenessContext()
    assert user32.AreDpiAwarenessContextsEqual(before, after)


@windows_only
def test_windows_a_real_grab_is_a_picture_or_an_honest_refusal() -> None:
    cap = GdiCapturer()
    focus = cap.focus("screen")
    try:
        img = cap.grab("screen", window_id=focus.window_id)
    except CaptureRefused as exc:
        # A CI runner's desktop may be locked or blank; that must be a refusal.
        assert exc.refusal.code == "capture_failed"
        return
    assert 0 < img.width <= MAX_SIDE and 0 < img.height <= MAX_SIDE
    assert encode_png(img).startswith(b"\x89PNG")
