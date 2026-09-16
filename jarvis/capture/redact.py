"""Take the secrets out before the bytes leave the machine, and say which method.

THE ASYMMETRY THIS MODULE IS BUILT AROUND. Text can be redacted *exactly*: a
secret is a literal string, ``str.replace`` removes every occurrence of it, and
afterwards you can PROVE it is gone by looking for it again. Pixels cannot. There
is no cheap, reliable way to find "the API key" in a bitmap — OCR is a
dependency, a latency cost and, worse, a source of false confidence, because an
OCR pass that misses one line looks exactly like an OCR pass that missed nothing.
So the two paths are not two implementations of one idea; they are a good option
and a bad one, and the API is shaped to make the good one the easy one.

WHY A BOX AND NEVER A BLUR. A blur is a low-pass filter. It is lossy in practice
and reversible in principle — deconvolution against a known kernel, or simply
brute-forcing the small space of strings that blur to the same thing, has
recovered pixelated text often enough to be a genre of news story. More
important than whether any particular blur is breakable is what it *looks* like:
a blurred key looks redacted, so nobody checks it again. An opaque rectangle is
the whole point — the pixels underneath are gone, replaced, not attenuated.

WHERE SECRETS COME FROM. The caller passes them in, as a
:class:`jarvis.bus.Redactor`. This module deliberately does not import
``keyring`` and has no module-level "loaded secrets" slot, for exactly the reason
:mod:`jarvis.bus` gives: that would be process-global mutable state deciding,
by import order, whether a token reaches a chat server. One seam, used twice.

THE SECOND LAYER IS SHAPE. A literal list only covers the credentials Jarvis
itself holds. A terminal showing ``env`` also shows the user's *other* tokens —
a colleague's, a CI runner's, one pasted from a ticket five minutes ago — and
none of those are in the keyring. So the text path also masks anything matching
a high-confidence credential SHAPE. That is a net, not a guarantee, and it is
labelled as one everywhere it appears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.bus import Redactor
from jarvis.capture.artifact import RedactionReport
from jarvis.capture.png import FONT_HEIGHT, Box, Canvas, RawImage, text_width

__all__ = [
    "REDACTION_FILL",
    "SECRET_SHAPES",
    "Finding",
    "banner",
    "redact_pixels",
    "redact_text",
    "scan_shapes",
    "surviving_secrets",
]

#: Opaque, and deliberately not black: a black box on a dark terminal is easy to
#: mistake for the terminal. Magenta is not a colour any real screen produces by
#: accident, so "was this redacted?" is answerable across a room.
REDACTION_FILL = (255, 0, 255)

#: High-confidence credential shapes only. Every pattern here has a fixed,
#: vendor-assigned prefix or an unmistakable framing, because a loose pattern
#: (say, "thirty-two hex characters") would mask git SHAs, checksums and test
#: fixtures until the transcript was unreadable and people started turning the
#: whole thing off. Under-matching here is recoverable; making the honest path
#: annoying is not.
SECRET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("google_key", re.compile(r"AIza[A-Za-z0-9_\-]{30,}")),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # The env-dump case: `export ANTHROPIC_API_KEY=…`, `DATABASE_PASSWORD=…`.
    # Anchored on the NAME, because the value of an unknown vendor's token has
    # no shape at all and this is the only thing that ever will identify it.
    (
        "env_assignment",
        re.compile(
            r"\b[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY)"
            r"\s*[=:]\s*(?P<value>\S+)"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A shaped secret, located but NEVER quoted.

    The value is not carried on this object on purpose. A finding ends up in an
    effect summary and in the activity log, and a "helpful" excerpt is how the
    redaction leaks the very thing it removed.
    """

    name: str
    start: int
    end: int


def scan_shapes(text: str) -> tuple[Finding, ...]:
    """Locate shaped credentials, outermost-first, with overlaps collapsed."""
    found: list[Finding] = []
    for name, pattern in SECRET_SHAPES:
        for m in pattern.finditer(text):
            # env_assignment brackets the whole `NAME=value`; only the value is
            # secret, and masking the name too would hide WHICH credential was
            # on screen, which is the one detail a person needs afterwards.
            span = m.span("value") if "value" in m.re.groupindex else m.span()
            found.append(Finding(name, span[0], span[1]))

    found.sort(key=lambda f: (f.start, -f.end))
    kept: list[Finding] = []
    for f in found:
        if kept and f.start < kept[-1].end:
            continue
        kept.append(f)
    return tuple(kept)


def redact_text(
    text: str,
    *,
    redactor: Redactor,
    mask_shapes: bool = True,
) -> tuple[str, RedactionReport]:
    """Remove every known literal, then every shaped credential. Exact, by construction.

    The literal pass is :meth:`jarvis.bus.Redactor._walk`'s string branch with a
    counter bolted on — the ordering that makes it correct (longest secret first,
    so a short secret that is a substring of a long one cannot leave a
    recognisable tail) still lives in :meth:`jarvis.bus.Redactor.of`, which is
    the only place it should. The count is needed here and nowhere else: the
    effect summary says how many credentials were removed, and "some" is not a
    thing a user can check.
    """
    out = text
    literals = 0
    # Longest first, re-asserted here rather than trusted. `Redactor.of` already
    # orders this way, but a `Redactor(secrets=(...))` built by hand does not,
    # and replacing a short secret that is a prefix of a long one first leaves a
    # recognisable tail of the long one that `surviving_secrets` can no longer
    # see — the gate below would then pass on text that still leaks.
    for secret in sorted(redactor.secrets, key=len, reverse=True):
        n = out.count(secret)
        if n:
            out = out.replace(secret, redactor.placeholder)
            literals += n

    shapes: list[str] = []
    if mask_shapes:
        for f in reversed(scan_shapes(out)):
            # `FOO_TOKEN=[redacted]` still matches env_assignment, and masking
            # the mask again would both double-count and replace a readable
            # placeholder with a less readable one.
            if out[f.start : f.end] == redactor.placeholder:
                continue
            out = f"{out[: f.start]}[redacted:{f.name}]{out[f.end :]}"
            shapes.append(f.name)
        shapes.reverse()

    # Belt and braces, and cheap: this is the assertion that the whole text-first
    # argument rests on, so it is made rather than assumed. A survivor here means
    # the replacement above is wrong, not that the text was unlucky.
    left = surviving_secrets(out, redactor)
    if left:
        raise AssertionError(
            f"{len(left)} secret literal(s) survived redaction; refusing to hand back the text"
        )

    return out, RedactionReport(
        exact=True,
        method="string_replacement" if (literals or shapes) else "none",
        literals_removed=literals,
        shapes_removed=tuple(shapes),
        residual_risk=(
            "Shape matching is a net, not a proof: a credential with no recognisable prefix "
            "and no telltale variable name is not detected."
            if mask_shapes
            else "Shape matching was switched off; only the supplied literals were removed."
        ),
    )


def surviving_secrets(text: str, redactor: Redactor) -> tuple[str, ...]:
    """Which supplied literals are STILL present. Returns names, never values.

    Used as a pre-delivery gate by :mod:`jarvis.capture.service`. Returns the
    sha-free index of each surviving secret rather than the secret itself for
    the same reason :class:`Finding` carries no value.
    """
    return tuple(f"secret[{i}]" for i, s in enumerate(redactor.secrets) if s and s in text)


def redact_pixels(
    img: RawImage,
    boxes: tuple[Box, ...] | list[Box],
    *,
    fill: tuple[int, int, int] = REDACTION_FILL,
) -> tuple[RawImage, RedactionReport]:
    """Paint opaque rectangles over the named regions. Nothing else is examined.

    This CANNOT be exact and the report says so. The caller names regions it
    already knows about — the tab strip holding a filename, the pane where the
    environment was dumped — and everything outside them goes out as it was
    photographed. That is why :attr:`RedactionReport.exact` is False here
    unconditionally and why the picture also gets a :func:`banner`.
    """
    canvas = Canvas.of(img)
    pixels = sum(canvas.fill(b, fill) for b in boxes)
    return canvas.freeze(), RedactionReport(
        exact=False,
        method="region_fill" if boxes else "none",
        regions_filled=len(boxes),
        pixels_filled=pixels,
        residual_risk=(
            "Only the regions the caller named were covered. Anything else legible in this "
            "picture left the machine as it was on screen."
        ),
    )


def banner(
    img: RawImage,
    lines: tuple[str, ...] | list[str],
    *,
    background: tuple[int, int, int] = REDACTION_FILL,
    foreground: tuple[int, int, int] = (0, 0, 0),
    scale: int = 2,
) -> RawImage:
    """Burn a warning band across the top of the picture.

    In the pixels rather than only in the caption, because a caption is a
    separate field: it does not survive a forward, a crop, a "save image as", or
    a viewer that shows the file without the message it arrived in. The picture
    has to carry its own provenance.

    ``scale`` is a ceiling, not a demand: the largest size that fits is used,
    down to one pixel per dot. A terminal pane is narrower than a screen and a
    warning that silently vanished on the narrower one would be missing from
    exactly the captures most likely to be of a terminal.

    Silently no-ops on an image too small to hold the band even at scale 1. That
    is deliberate — the caller still gets the caption warning and the
    :class:`RedactionReport`, and refusing to deliver a 40x40 thumbnail because
    the warning would not fit would trade a real capability for a cosmetic one.
    """
    if not lines:
        return img
    for size in range(scale, 0, -1):
        pad = 2 * size
        row_h = (FONT_HEIGHT + 2) * size
        band = pad * 2 + row_h * len(lines)
        widest = max(text_width(line, scale=size) for line in lines)
        if band < img.height and widest + pad * 2 <= img.width:
            break
    else:
        return img

    canvas = Canvas.of(img)
    canvas.fill(Box(0, 0, img.width, band), background)
    for i, line in enumerate(lines):
        canvas.text(pad, pad + i * row_h, line, foreground, scale=size)
    return canvas.freeze()
