"""The icon by the clock: Open Jarvis, Restart voice, Quit.

OPTIONAL BY DESIGN. ``pystray`` and Pillow are the ``app`` extra, and on Linux
``import pystray`` does not merely fail without a display — it raises an Xlib
error that is not an ImportError. So anything at all going wrong while the
tray is built means "no tray": the app then simply waits for its quit event,
and the HUD's own Quit button still ends it.

THE ICON IS DRAWN, NOT LOADED. An arc-reactor ring in cyan on near-black, made
with Pillow at run time, so the frozen build has no image file to lose and the
icon cannot drift from the one the HUD draws in SVG.
"""

from __future__ import annotations

import math
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

__all__ = ["CYAN", "NAVY", "icon_image", "load_pystray", "run"]

CYAN = (79, 209, 255)
NAVY = (6, 12, 24)


def icon_image(size: int = 64) -> Any:
    """The arc reactor: a dark disc, a segmented outer ring, a bright core. Needs Pillow."""
    from PIL import Image, ImageDraw

    # Drawn at 4x and scaled down: Pillow's shapes have no anti-aliasing, and a
    # 16 px tray icon drawn directly is all staircase.
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = big / 2

    def ring(r: float) -> list[float]:
        return [c - r, c - r, c + r, c + r]

    d.ellipse(ring(big * 0.48), fill=(*NAVY, 255))
    d.ellipse(ring(big * 0.44), outline=(*CYAN, 140), width=max(1, big // 48))
    # Ten segments around the ring, the gaps between them as wide as a coil.
    for i in range(10):
        start = i * 36 + 6
        d.arc(ring(big * 0.36), start, start + 24, fill=(*CYAN, 255), width=max(2, big // 12))
    d.ellipse(ring(big * 0.22), outline=(*CYAN, 200), width=max(1, big // 40))
    # The core: concentric discs fading inwards to white.
    steps = 6
    for i in range(steps):
        t = i / (steps - 1)
        r = big * (0.17 - 0.11 * t)
        mix = tuple(round(CYAN[k] + (255 - CYAN[k]) * t) for k in range(3))
        d.ellipse(ring(r), fill=(*mix, round(160 + 95 * t)))
    # Three short struts tie the core to the ring.
    for k in range(3):
        a = math.radians(90 + k * 120)
        x0, y0 = c + math.cos(a) * big * 0.17, c - math.sin(a) * big * 0.17
        x1, y1 = c + math.cos(a) * big * 0.30, c - math.sin(a) * big * 0.30
        d.line([x0, y0, x1, y1], fill=(*CYAN, 220), width=max(1, big // 32))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def load_pystray() -> Any | None:
    """The pystray module, or None when this machine cannot show a tray icon."""
    try:
        import pystray  # noqa: PLC0415 - optional, and its import can raise anything
    except Exception:  # noqa: BLE001 - see the module docstring: not only ImportError
        return None
    return pystray


def run(
    *,
    open_window: Callable[[], Any],
    restart_voice: Callable[[], Any],
    quit: Callable[[], Any],  # noqa: A002 - the menu item's own name
    stop: threading.Event,
    title: str = "Jarvis",
    pystray: Any | None = None,
    poll_s: float = 0.5,
) -> str:
    """Run the tray on THIS thread until ``stop`` is set. Returns ``"tray"`` or ``"headless"``.

    pystray must own the main thread on macOS and is happiest there everywhere,
    so the caller runs this last, on the main thread, and does its teardown
    when it returns. ``quit`` should set ``stop`` (directly or not); a watcher
    thread turns ``stop`` into ``icon.stop()`` whoever set it — the menu, or
    the HUD's Quit button.
    """
    tray = pystray if pystray is not None else load_pystray()
    if tray is not None:
        try:
            icon = _build(tray, open_window, restart_voice, quit, title)
        except Exception:  # noqa: BLE001 - a tray that cannot be built is no tray
            traceback.print_exc(file=sys.stderr)
            icon = None
        if icon is not None:

            def watch() -> None:
                stop.wait()
                icon.stop()

            threading.Thread(target=watch, name="tray-watch", daemon=True).start()
            try:
                icon.run()
            except Exception:  # noqa: BLE001 - e.g. no notification area at all
                traceback.print_exc(file=sys.stderr)
            if stop.is_set():
                return "tray"
            # The icon went away without anybody quitting. The app is still
            # wanted; keep it alive and quittable from the HUD.
    # A loop rather than one wait(): on Windows an untimed Event.wait() cannot
    # be interrupted by Ctrl+C, and a developer running this in a terminal
    # expects Ctrl+C to work.
    while not stop.wait(poll_s):
        pass
    return "headless"


def _build(
    tray: Any,
    open_window: Callable[[], Any],
    restart_voice: Callable[[], Any],
    quit: Callable[[], Any],  # noqa: A002
    title: str,
) -> Any:
    def call(fn: Callable[[], Any]) -> Callable[..., None]:
        # pystray calls menu actions with (icon, item); ours take nothing. A
        # failure is logged, never raised into pystray's own loop.
        def action(*_: Any) -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)

        return action

    menu = tray.Menu(
        tray.MenuItem("Open Jarvis", call(open_window), default=True),
        tray.MenuItem("Restart voice", call(restart_voice)),
        tray.Menu.SEPARATOR,
        tray.MenuItem("Quit", call(quit)),
    )
    return tray.Icon("jarvis", icon_image(64), title, menu)
