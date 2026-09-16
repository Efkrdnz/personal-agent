"""PNG in and out, and a five-pixel font, with nothing but the standard library.

WHY A HAND-ROLLED CODEC INSTEAD OF PILLOW. Three reasons, in order of weight.
First, the only pixel operation this package performs is *fill a rectangle with
an opaque colour* — a redaction is a solid box, never a blur (see
:mod:`jarvis.capture.redact`) — and that does not justify a compiled dependency
on every machine that may ever want to send a screenshot. Second, the external
capture tools on every platform emit PNG on stdout or into a file, so something
here has to turn PNG into pixels before anything can be drawn over it; the
alternative is piping the picture through a second process and trusting it.
Third, and decisively: this must be *deterministic*, because the synthetic
capturer is the only capturer CI will ever run, and a test that asserts "the
secret region is now solid black" has to be able to read the bytes back.

WHAT IS AND IS NOT SUPPORTED, so the failure is named rather than discovered.
Encoding writes 8-bit truecolour, non-interlaced, filter type 0 on every
scanline — the shape every viewer has understood since 1996, chosen because a
predictable encoder is worth more here than a small file. Decoding accepts 8-bit
greyscale, greyscale+alpha, truecolour and truecolour+alpha, non-interlaced,
with all five scanline filters, which covers what ``maim``, ``grim``, ``import``,
``scrot`` and macOS ``screencapture`` actually produce. Anything else — 16-bit
channels, a palette, Adam7 interlacing — raises :class:`UnsupportedPNG` and the
caller REFUSES to deliver the picture rather than delivering one it could not
draw over. That refusal is the point: an undecodable screenshot is an
unredactable screenshot.

The font exists so a warning can be burned into the pixels themselves. A caption
travels beside the file and can be scrolled past, cropped out of a forward, or
lost when someone saves the image; the band across the top of the picture cannot.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

__all__ = [
    "BLACK",
    "FONT_HEIGHT",
    "FONT_WIDTH",
    "PNG_SIGNATURE",
    "WHITE",
    "Box",
    "Canvas",
    "RawImage",
    "UnsupportedPNG",
    "decode_png",
    "encode_png",
    "looks_blank",
    "text_width",
]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

#: Channels per pixel in a :class:`RawImage`. Alpha is dropped at decode time on
#: purpose: a screenshot with a transparent region that a viewer composites over
#: white is a screenshot whose redaction box depends on the viewer.
CHANNELS = 3

FONT_WIDTH = 5
FONT_HEIGHT = 7


class UnsupportedPNG(ValueError):
    """A PNG this module will not turn into pixels.

    Its own type because the caller's response is specific and non-obvious: not
    "retry", not "send it anyway", but "refuse, because a picture that cannot be
    decoded cannot be drawn over, and an unredacted screenshot is the one thing
    this package exists to prevent".
    """


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned rectangle in pixels, top-left origin.

    Lives here rather than in :mod:`jarvis.capture.redact` because both the
    redactor and the canvas need it and neither should import the other.
    """

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError(f"a box cannot have negative extent: {self!r}")

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class RawImage:
    """Decoded pixels: 8-bit RGB, row-major, no padding between rows.

    Immutable because a captured frame is evidence. Drawing produces a NEW
    image through :class:`Canvas`, so "the picture before redaction" and "the
    picture that left the machine" are two distinct objects and a test can
    compare them.
    """

    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"an image needs a positive extent, got {self.width}x{self.height}")
        expected = self.width * self.height * CHANNELS
        if len(self.pixels) != expected:
            raise ValueError(
                f"{self.width}x{self.height} RGB needs {expected} bytes, got {len(self.pixels)}"
            )

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * CHANNELS
        return (self.pixels[i], self.pixels[i + 1], self.pixels[i + 2])


def encode_png(img: RawImage, *, compress_level: int = 6) -> bytes:
    """Serialise to 8-bit truecolour PNG with filter 0 on every scanline.

    ``compress_level`` is an explicit argument rather than zlib's default so the
    output is byte-identical across interpreter versions; a test that pins a
    digest is pinning this function, not CPython's idea of a good default.
    """
    raw = bytearray()
    stride = img.width * CHANNELS
    for y in range(img.height):
        raw.append(0)  # filter type 0: None
        raw += img.pixels[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", img.width, img.height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            PNG_SIGNATURE,
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", zlib.compress(bytes(raw), compress_level)),
            _chunk(b"IEND", b""),
        )
    )


def decode_png(data: bytes) -> RawImage:
    """Turn PNG bytes into RGB pixels, or refuse loudly."""
    if not data.startswith(PNG_SIGNATURE):
        raise UnsupportedPNG("not a PNG: the eight-byte signature is missing")

    header: tuple[int, ...] | None = None
    idat = bytearray()
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) != length:
            raise UnsupportedPNG(f"chunk {kind!r} is truncated")
        pos += 12 + length  # 4 length + 4 type + body + 4 CRC
        if kind == b"IHDR":
            # struct.error is not a ValueError, so an IHDR of the wrong size
            # would escape every `except ValueError` between here and the
            # caller and arrive as an untyped crash instead of a refusal.
            if len(body) != 13:
                raise UnsupportedPNG(f"IHDR is {len(body)} bytes, expected 13")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    if header is None:
        raise UnsupportedPNG("no IHDR chunk")
    width, height, depth, colour, compression, filt, interlace = header
    if depth != 8:
        raise UnsupportedPNG(f"only 8 bits per channel is supported, got {depth}")
    if colour not in (0, 2, 4, 6):
        raise UnsupportedPNG(f"colour type {colour} (palette or unknown) is not supported")
    if compression != 0 or filt != 0:
        raise UnsupportedPNG("non-standard compression or filter method")
    if interlace != 0:
        raise UnsupportedPNG("Adam7 interlacing is not supported")
    if width == 0 or height == 0:
        raise UnsupportedPNG("a zero-extent image has nothing to redact")

    src_channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour]
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise UnsupportedPNG(f"IDAT will not decompress: {exc}") from exc

    lines = _unfilter(raw, width, height, src_channels)
    return RawImage(width, height, _to_rgb(lines, width, height, src_channels))


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    """Reverse the per-scanline filters, in place, into one flat buffer."""
    stride = width * channels
    if len(raw) != (stride + 1) * height:
        raise UnsupportedPNG(
            f"decompressed IDAT is {len(raw)} bytes, expected {(stride + 1) * height}"
        )
    out = bytearray(stride * height)
    prev_off = -1
    for y in range(height):
        ftype = raw[y * (stride + 1)]
        line = raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]
        off = y * stride
        # The filter type is per scanline, so it is dispatched per scanline. The
        # obvious shape — one loop over bytes with the branch inside — costs five
        # comparisons on every one of the ~6M bytes of a 1080p frame, and this
        # decode sits between a 10s capture timeout and the user.
        if ftype == 0:
            out[off : off + stride] = line
        elif ftype == 1:
            # The leading pixel first: filter 1 reads the byte `channels` to its
            # left, so writing it after the loop would feed the loop zeroes.
            out[off : off + channels] = line[:channels]
            for i in range(channels, stride):
                out[off + i] = (line[i] + out[off + i - channels]) & 0xFF
        elif ftype == 2:
            if prev_off < 0:
                out[off : off + stride] = line
            else:
                for i in range(stride):
                    out[off + i] = (line[i] + out[prev_off + i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = out[off + i - channels] if i >= channels else 0
                b = out[prev_off + i] if prev_off >= 0 else 0
                out[off + i] = (line[i] + ((a + b) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = out[off + i - channels] if i >= channels else 0
                b = out[prev_off + i] if prev_off >= 0 else 0
                c = out[prev_off + i - channels] if (prev_off >= 0 and i >= channels) else 0
                out[off + i] = (line[i] + _paeth(a, b, c)) & 0xFF
        else:
            raise UnsupportedPNG(f"unknown scanline filter {ftype}")
        prev_off = off
    return out


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _to_rgb(lines: bytearray, width: int, height: int, channels: int) -> bytes:
    if channels == 3:
        return bytes(lines)
    out = bytearray(width * height * CHANNELS)
    for i in range(width * height):
        src = i * channels
        dst = i * CHANNELS
        if channels in (1, 2):  # grey, grey+alpha
            g = lines[src]
            out[dst] = out[dst + 1] = out[dst + 2] = g
        else:  # RGBA — alpha discarded, see the note on CHANNELS
            out[dst : dst + 3] = lines[src : src + 3]
    return bytes(out)


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def looks_blank(img: RawImage) -> bool:
    """Every pixel the same colour.

    A heuristic with one specific job: macOS returns a *successful* capture of a
    featureless desktop when Screen Recording permission has not been granted,
    rather than an error, so "the call worked" is not evidence that anything was
    photographed. See :mod:`jarvis.capture.backends`.
    """
    first = img.pixels[:CHANNELS]
    return img.pixels == first * (img.width * img.height)


class Canvas:
    """A mutable drawing surface. The only thing in this package that mutates.

    Deliberately not a dataclass and deliberately not shared: one is created,
    drawn on, and frozen back into a :class:`RawImage` within a single function
    call, which is what keeps "no module-level mutable state" true while still
    letting a redaction be a loop over bytes rather than a rope of copies.
    """

    __slots__ = ("width", "height", "_buf")

    def __init__(self, width: int, height: int, buf: bytearray) -> None:
        self.width = width
        self.height = height
        self._buf = buf

    @classmethod
    def of(cls, img: RawImage) -> Canvas:
        return cls(img.width, img.height, bytearray(img.pixels))

    @classmethod
    def blank(cls, width: int, height: int, colour: tuple[int, int, int] = BLACK) -> Canvas:
        return cls(width, height, bytearray(bytes(colour) * (width * height)))

    def freeze(self) -> RawImage:
        return RawImage(self.width, self.height, bytes(self._buf))

    def fill(self, box: Box, colour: tuple[int, int, int]) -> int:
        """Paint an OPAQUE rectangle. Returns the number of pixels changed.

        Clipped to the image rather than raising: a region derived from a window
        geometry that has since moved must still redact whatever part of it is
        on screen, and refusing the whole redaction because one box hangs over
        the edge would be the worst possible failure mode.
        """
        x0 = max(0, box.x)
        y0 = max(0, box.y)
        x1 = min(self.width, box.x + box.width)
        y1 = min(self.height, box.y + box.height)
        if x1 <= x0 or y1 <= y0:
            return 0
        row = bytes(colour) * (x1 - x0)
        for y in range(y0, y1):
            start = (y * self.width + x0) * CHANNELS
            self._buf[start : start + len(row)] = row
        return (x1 - x0) * (y1 - y0)

    def text(
        self,
        x: int,
        y: int,
        message: str,
        colour: tuple[int, int, int] = WHITE,
        *,
        scale: int = 2,
    ) -> int:
        """Draw uppercase 5x7 text. Returns the x just past the last glyph."""
        if scale < 1:
            raise ValueError("scale must be at least 1")
        cx = x
        for ch in message.upper():
            glyph = _GLYPHS.get(ch, _FALLBACK)
            for row, bits in enumerate(glyph):
                for col in range(FONT_WIDTH):
                    if bits[col] == "1":
                        self.fill(
                            Box(cx + col * scale, y + row * scale, scale, scale),
                            colour,
                        )
            cx += (FONT_WIDTH + 1) * scale
        return cx


def text_width(message: str, *, scale: int = 2) -> int:
    """How wide :meth:`Canvas.text` will draw this, including the trailing gap."""
    return len(message) * (FONT_WIDTH + 1) * scale


# A 5x7 bitmap font, one row of the glyph per string. Uppercase, digits and the
# four punctuation marks the warning band and a timestamp need — nothing else,
# because every glyph here is a line of source that has to be read to be
# trusted. Unknown characters become a filled block, which is louder than
# dropping them and impossible to mistake for successful rendering.
_GLYPHS: dict[str, tuple[str, ...]] = {
    ch: tuple(rows.split("/"))
    for ch, rows in {
        "A": "01110/10001/10001/11111/10001/10001/10001",
        "B": "11110/10001/10001/11110/10001/10001/11110",
        "C": "01110/10001/10000/10000/10000/10001/01110",
        "D": "11110/10001/10001/10001/10001/10001/11110",
        "E": "11111/10000/10000/11110/10000/10000/11111",
        "F": "11111/10000/10000/11110/10000/10000/10000",
        "G": "01110/10001/10000/10111/10001/10001/01111",
        "H": "10001/10001/10001/11111/10001/10001/10001",
        "I": "01110/00100/00100/00100/00100/00100/01110",
        "J": "00111/00010/00010/00010/00010/10010/01100",
        "K": "10001/10010/10100/11000/10100/10010/10001",
        "L": "10000/10000/10000/10000/10000/10000/11111",
        "M": "10001/11011/10101/10101/10001/10001/10001",
        "N": "10001/11001/10101/10011/10001/10001/10001",
        "O": "01110/10001/10001/10001/10001/10001/01110",
        "P": "11110/10001/10001/11110/10000/10000/10000",
        "Q": "01110/10001/10001/10001/10101/10010/01101",
        "R": "11110/10001/10001/11110/10100/10010/10001",
        "S": "01111/10000/10000/01110/00001/00001/11110",
        "T": "11111/00100/00100/00100/00100/00100/00100",
        "U": "10001/10001/10001/10001/10001/10001/01110",
        "V": "10001/10001/10001/10001/10001/01010/00100",
        "W": "10001/10001/10001/10101/10101/11011/10001",
        "X": "10001/10001/01010/00100/01010/10001/10001",
        "Y": "10001/10001/01010/00100/00100/00100/00100",
        "Z": "11111/00001/00010/00100/01000/10000/11111",
        "0": "01110/10001/10011/10101/11001/10001/01110",
        "1": "00100/01100/00100/00100/00100/00100/01110",
        "2": "01110/10001/00001/00010/00100/01000/11111",
        "3": "11111/00010/00100/00010/00001/10001/01110",
        "4": "00010/00110/01010/10010/11111/00010/00010",
        "5": "11111/10000/11110/00001/00001/10001/01110",
        "6": "00110/01000/10000/11110/10001/10001/01110",
        "7": "11111/00001/00010/00100/01000/01000/01000",
        "8": "01110/10001/10001/01110/10001/10001/01110",
        "9": "01110/10001/10001/01111/00001/00010/01100",
        " ": "00000/00000/00000/00000/00000/00000/00000",
        "-": "00000/00000/00000/11111/00000/00000/00000",
        ".": "00000/00000/00000/00000/00000/01100/01100",
        ":": "00000/01100/01100/00000/01100/01100/00000",
        "/": "00001/00010/00010/00100/01000/01000/10000",
    }.items()
}

_FALLBACK: tuple[str, ...] = tuple(["11111"] * FONT_HEIGHT)
