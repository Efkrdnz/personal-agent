#!/usr/bin/env python3
"""Draw the arc-reactor icon and write ``packaging/jarvis.ico``.

    python packaging/make_icon.py                # rewrite packaging/jarvis.ico
    python packaging/make_icon.py --png out/     # also write one PNG per size, to look at

GENERATED ART, COMMITTED. The .ico is the output of this file and nothing else,
so there is no licence question about it and no designer to ask for a 20 px
variant: change the drawing here, run it, commit both.

EVERY SIZE IS DRAWN, NOT SHRUNK. Windows picks a different frame for the
taskbar (24/32), Explorer's details view (16), its tiles (48/64) and the
Alt-Tab switcher (256). A 256 px reactor scaled to 16 px is a grey smudge, so
the small frames are a simpler drawing — a solid coil and a bigger core — and
only the large ones get the segmented coil, the tick ring and the glow.

The colours are the HUD's (``jarvis/window/static/app.css``): the icon on the
desktop, the one by the clock and the orb in the window should read as one
object.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

__all__ = ["SIZES", "draw", "main", "write_ico"]

#: Every frame Windows asks an .ico for, from Explorer's details view up to Alt-Tab.
SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)

CYAN = (79, 209, 255)
CYAN_HI = (169, 233, 255)
NAVY = (6, 17, 31)
VOID = (2, 6, 13)

# Pillow's primitives are not anti-aliased; drawing at 4x and resampling down is.
_SUPERSAMPLE = 4

OUT = Path(__file__).resolve().parent / "jarvis.ico"


def _mix(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def draw(size: int) -> Image.Image:
    """One frame of the icon, ``size`` pixels square, RGBA on transparent."""
    big = size * _SUPERSAMPLE
    c = big / 2
    small = size <= 24
    detailed = size >= 64

    def box(r: float) -> list[float]:
        return [c - r * big, c - r * big, c + r * big, c + r * big]

    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # The housing: a disc darkening outwards, so the core reads as the light source.
    steps = 6 if small else 24
    for i in range(steps):
        t = i / (steps - 1)
        d.ellipse(box(0.49 - 0.27 * t), fill=(*_mix(VOID, NAVY, t), 255))
    d.ellipse(box(0.47), outline=(*CYAN, 120), width=max(1, round(big * 0.012)))

    # Everything bright goes on its own layer, so the glow is that layer blurred.
    light = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ld = ImageDraw.Draw(light)

    if detailed:
        # A fine tick ring between the bezel and the coil, the HUD's instrument look.
        for k in range(60):
            a = math.radians(k * 6)
            r0, r1 = (0.405, 0.435) if k % 5 else (0.395, 0.44)
            ld.line(
                [
                    c + math.cos(a) * big * r0,
                    c - math.sin(a) * big * r0,
                    c + math.cos(a) * big * r1,
                    c - math.sin(a) * big * r1,
                ],
                fill=(*CYAN, 150 if k % 5 else 230),
                width=max(1, round(big * 0.006)),
            )

    if small:
        # At 16-24 px ten segments are ten grey pixels and an inner ring merges
        # into the core. A solid coil, a dark gap and a dot is what still reads.
        ld.ellipse(box(0.375), outline=(*CYAN, 255), width=round(big * 0.1))
    else:
        # Ten coil segments with gaps the width of a coil, starting off-axis so
        # no gap sits at twelve o'clock where the eye lands first.
        for k in range(10):
            start = k * 36 + 9
            ld.arc(box(0.355), start, start + 26, fill=(*CYAN, 255), width=round(big * 0.08))
        ld.ellipse(box(0.235), outline=(*CYAN_HI, 220), width=max(1, round(big * 0.018)))

    # The core: concentric discs going white at the centre.
    core = 0.2 if small else 0.165
    rings = 5 if small else 12
    for i in range(rings):
        t = i / (rings - 1)
        ld.ellipse(box(core * (1 - 0.72 * t)), fill=(*_mix(CYAN, (255, 255, 255), t), 255))

    if size >= 40:
        glow = light.filter(ImageFilter.GaussianBlur(big * 0.035))
        img.alpha_composite(glow)
    img.alpha_composite(light)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: Path, sizes: tuple[int, ...] = SIZES) -> list[Image.Image]:
    """Write every size as its own frame. Returns the frames, largest first."""
    frames = [draw(s) for s in sorted(sizes, reverse=True)]
    # Pillow writes one frame per entry of ``sizes``, taking each from
    # ``append_images`` when one of that exact size is given, and otherwise
    # by SHRINKING the first image — the smudge this file exists to avoid.
    frames[0].save(
        path,
        format="ICO",
        sizes=[(f.width, f.height) for f in frames],
        append_images=frames[1:],
    )
    return frames


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=OUT, help="the .ico to write")
    ap.add_argument("--png", type=Path, default=None, help="also write PNGs here, to look at")
    args = ap.parse_args(argv)

    frames = write_ico(args.out)
    if args.png is not None:
        args.png.mkdir(parents=True, exist_ok=True)
        for f in frames:
            f.save(args.png / f"jarvis-{f.width}.png")
    sizes = ", ".join(str(f.width) for f in reversed(frames))
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes; {sizes} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
