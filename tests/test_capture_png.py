"""The codec, tested as the load-bearing thing it is.

If :func:`jarvis.capture.png.decode_png` is wrong, a redaction box lands in the
wrong place and the secret goes out beside a magenta rectangle that looks like
it worked. So every scanline filter a real screenshot tool might emit is built
here by hand and decoded, rather than trusted because a round trip through our
own encoder passed.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from jarvis.capture.png import (
    PNG_SIGNATURE,
    Box,
    Canvas,
    RawImage,
    UnsupportedPNG,
    decode_png,
    encode_png,
    looks_blank,
    text_width,
)


def gradient(width: int = 7, height: int = 5) -> RawImage:
    """Not a flat colour: a filter bug on a flat image decodes perfectly."""
    buf = bytearray()
    for y in range(height):
        for x in range(width):
            buf += bytes(((x * 31) % 256, (y * 57 + x) % 256, (x * y * 13) % 256))
    return RawImage(width, height, bytes(buf))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def handmade_png(
    img: RawImage,
    *,
    filter_type: int = 0,
    colour: int = 2,
    depth: int = 8,
    interlace: int = 0,
    channels: int = 3,
    pixels: bytes | None = None,
) -> bytes:
    """A PNG built the way an external tool builds one, not the way we do."""
    data = pixels if pixels is not None else img.pixels
    stride = img.width * channels
    raw = bytearray()
    prev = bytes(stride)
    for y in range(img.height):
        line = data[y * stride : (y + 1) * stride]
        raw.append(filter_type)
        for i, x in enumerate(line):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                v = x
            elif filter_type == 1:
                v = x - a
            elif filter_type == 2:
                v = x - b
            elif filter_type == 3:
                v = x - (a + b) // 2
            else:
                v = x - _paeth(a, b, c)
            raw.append(v & 0xFF)
        prev = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", img.width, img.height, depth, colour, 0, 0, interlace)
    return b"".join(
        (
            PNG_SIGNATURE,
            chunk(b"IHDR", ihdr),
            chunk(b"IDAT", zlib.compress(bytes(raw))),
            chunk(b"IEND", b""),
        )
    )


def test_round_trip_is_lossless() -> None:
    img = gradient()
    assert decode_png(encode_png(img)).pixels == img.pixels


def test_encoding_is_deterministic() -> None:
    # A test that pins a digest is pinning encode_png, not zlib's mood.
    img = gradient()
    assert encode_png(img) == encode_png(img)


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_every_scanline_filter_decodes(filter_type: int) -> None:
    img = gradient(9, 6)
    assert decode_png(handmade_png(img, filter_type=filter_type)).pixels == img.pixels


def test_greyscale_becomes_rgb() -> None:
    img = gradient(4, 3)
    grey = bytes(((x * 40) % 256) for x in range(12))
    data = handmade_png(img, colour=0, channels=1, pixels=grey, filter_type=4)
    out = decode_png(data)
    assert out.pixel(0, 0) == (grey[0],) * 3
    assert out.pixel(3, 2) == (grey[11],) * 3


def test_alpha_is_dropped_not_composited() -> None:
    # A transparent region must not become "whatever the viewer paints behind
    # it" — a redaction box whose colour depends on the viewer is not a box.
    img = gradient(2, 2)
    rgba = bytes([10, 20, 30, 0, 40, 50, 60, 255, 70, 80, 90, 128, 1, 2, 3, 7])
    out = decode_png(handmade_png(img, colour=6, channels=4, pixels=rgba))
    assert out.pixel(0, 0) == (10, 20, 30)
    assert out.pixel(1, 1) == (1, 2, 3)


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"depth": 16}, "16 bits"),
        ({"colour": 3}, "palette"),
        ({"interlace": 1}, "Adam7"),
    ],
)
def test_named_refusals_rather_than_wrong_pixels(kwargs: dict[str, int], why: str) -> None:
    img = gradient(4, 4)
    with pytest.raises(UnsupportedPNG):
        decode_png(handmade_png(img, **kwargs))


def test_not_a_png_at_all() -> None:
    with pytest.raises(UnsupportedPNG):
        decode_png(b"GIF89a and the rest")


def test_truncated_idat_is_refused_not_guessed() -> None:
    data = bytearray(encode_png(gradient()))
    with pytest.raises(UnsupportedPNG):
        decode_png(bytes(data[: len(data) // 2]))


def test_fill_is_clipped_and_counts_pixels() -> None:
    canvas = Canvas.blank(10, 10, (1, 2, 3))
    # Hanging over the edge must still redact what IS on screen.
    assert canvas.fill(Box(8, 8, 10, 10), (9, 9, 9)) == 4
    assert canvas.fill(Box(-5, -5, 3, 3), (9, 9, 9)) == 0
    img = canvas.freeze()
    assert img.pixel(9, 9) == (9, 9, 9)
    assert img.pixel(7, 7) == (1, 2, 3)


def test_text_draws_something_and_width_is_predictable() -> None:
    canvas = Canvas.blank(200, 30, (0, 0, 0))
    end = canvas.text(2, 2, "OK 1", (255, 255, 255), scale=2)
    assert end == 2 + text_width("OK 1", scale=2)
    assert not looks_blank(canvas.freeze())


def test_unknown_glyph_is_loud_not_silent() -> None:
    # Dropping an unrenderable character would let "NOT REDACTED" degrade into
    # something that still looks like a rendered warning.
    blocks = Canvas.blank(60, 20, (0, 0, 0))
    blocks.text(1, 1, "ç", (255, 255, 255), scale=1)
    assert not looks_blank(blocks.freeze())


def test_looks_blank_is_what_macos_returns_without_permission() -> None:
    assert looks_blank(Canvas.blank(8, 8, (60, 60, 60)).freeze())
    assert not looks_blank(gradient())


def test_an_image_must_describe_its_own_bytes() -> None:
    with pytest.raises(ValueError, match="needs"):
        RawImage(4, 4, b"\x00" * 10)
    with pytest.raises(ValueError, match="positive extent"):
        RawImage(0, 4, b"")
