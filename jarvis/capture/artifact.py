"""What leaves this package, and how it refuses to.

THE DELIVERY CONTRACT IS BYTES, A FILENAME AND A KIND — nothing else. This
package must not know whether the picture is going to Telegram, to the phone
leg's MMS fallback or to a local HUD, so it names no channel, imports no
channel, and returns an :class:`Artifact` that any of them can send. The
architecture's rule that ``sendDocument`` beats ``sendPhoto`` (``sendPhoto``
re-encodes to JPEG and smears terminal text) is a *channel* decision; what this
side owes the channel is :attr:`Artifact.lossless`, which says "re-encoding this
destroys the thing it was captured for" and lets the channel act on it.

A REFUSAL IS A VALUE, NOT A TIMEOUT. Every way this can fail — a Wayland consent
dialog nobody is standing next to, macOS Screen Recording never granted, a
policy that forbids photographing a ``.env`` — resolves to a typed
:class:`Refusal` carrying a code, a detail for the log and a remedy a human can
act on. The alternative is the failure this package was written to avoid: the
user says "send me a screenshot" from the bus, a permission dialog opens on a
screen in an empty room, and the request hangs until it expires with no
explanation anyone can read afterwards.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "MEDIA_MIME",
    "Artifact",
    "CaptureRefused",
    "MediaKind",
    "RedactionReport",
    "Refusal",
    "RefusalCode",
    "Subject",
]

#: What was photographed or rendered. See :mod:`jarvis.capture.policy` for why
#: ``transcript`` is the default and ``screen`` is not.
Subject = Literal["transcript", "pane", "screen"]

MediaKind = Literal["text", "image"]

MEDIA_MIME: dict[MediaKind, str] = {"text": "text/plain; charset=utf-8", "image": "image/png"}

RefusalCode = Literal[
    "no_display",
    "wayland_consent",
    "macos_permission",
    "no_backend",
    "capture_failed",
    "undecodable",
    "policy_forbidden",
    "subject_not_allowed",
    "pixels_disabled",
    "secret_visible",
    "too_large",
]

#: Said to the user verbatim. Module-owned text keyed by code, for the same
#: reason :mod:`jarvis.effects` owns its verdict templates: a sentence assembled
#: from a caller's string is a sentence nobody can audit for honesty.
_SPOKEN: dict[str, str] = {
    "no_display": "I can't take a screenshot — there's no desktop session I can see from here.",
    "wayland_consent": (
        "I can't take a screenshot while you're away. This desktop is Wayland, and every "
        "screenshot has to be approved in a dialog on that screen."
    ),
    "macos_permission": (
        "I can't take a screenshot — macOS hasn't given me Screen Recording permission, so all "
        "I'd send you is an empty desktop."
    ),
    "no_backend": "I can't take a screenshot — there's no screenshot tool installed I can drive.",
    "capture_failed": "I tried to take a screenshot and it failed.",
    "undecodable": (
        "I took the screenshot but I couldn't read the image back, so I couldn't check it for "
        "secrets. I'm not sending something I haven't looked at."
    ),
    "policy_forbidden": "I'm not going to photograph that — it's on the never-capture list.",
    "subject_not_allowed": "I'm not allowed to capture that much of the screen.",
    "pixels_disabled": "Screenshots are switched off; I can only send you text.",
    "secret_visible": (
        "There's something that looks like a credential on that screen, and I can't black it out "
        "in a picture. I'm not sending it."
    ),
    "too_large": "That capture came out too big to send.",
}


@dataclass(frozen=True, slots=True)
class Refusal:
    """A named no, with the remedy attached.

    ``detail`` is for the activity log and may contain caller text. ``spoken``
    is generated from the code alone and never interpolates any, so the sentence
    a user hears cannot be steered by a window title.
    """

    code: RefusalCode
    detail: str = ""
    remedy: str = ""

    @property
    def spoken(self) -> str:
        return _SPOKEN[self.code]


class CaptureRefused(RuntimeError):
    """Raised instead of returning a half-usable :class:`Artifact`.

    An exception rather than an ``Artifact | Refusal`` union because there is no
    partial success here: the caller either has bytes it may send or it has a
    sentence to say, and making the second one flow through the happy path is
    how an unredacted picture eventually gets sent by a caller that forgot to
    check a field.
    """

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(f"{refusal.code}: {refusal.detail or refusal.spoken}")
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class RedactionReport:
    """What was actually done to the bytes, and what could not be.

    :attr:`exact` is the field that matters. It is True only when every secret
    was removed by string replacement against a known literal — which is only
    ever possible on the text path. On pixels it is False, always, and that
    False is what puts the warning band in the picture and the warning in the
    caption. There is no third state: "probably clean" is the belief that gets
    an API key posted to a chat server.
    """

    exact: bool
    method: Literal["string_replacement", "region_fill", "none"]
    literals_removed: int = 0
    shapes_removed: tuple[str, ...] = ()
    regions_filled: int = 0
    pixels_filled: int = 0
    residual_risk: str = ""

    @property
    def anything_removed(self) -> bool:
        return bool(self.literals_removed or self.shapes_removed or self.regions_filled)


@dataclass(frozen=True, slots=True)
class Artifact:
    """Bytes, a filename, a kind — and the honest provenance of all three.

    :attr:`effect_id` is filled in after the capture is recorded in the ledger,
    so a channel that sends this can link its own ``telegram.send_document``
    effect back to the capture that produced it.
    """

    data: bytes
    filename: str
    media: MediaKind
    subject: Subject
    caption: str
    redaction: RedactionReport
    lossless: bool = True
    warnings: tuple[str, ...] = field(default_factory=tuple)
    effect_id: str | None = None

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("an empty artifact has nothing to deliver")
        # Validated HERE so no channel has to. This name is destined for a
        # multipart `Content-Disposition: … filename="…"` header, where a CR, an
        # LF or a quote is header injection rather than an odd filename, and for
        # a "save as" on the receiving device, where "." and ".." are a path.
        name = self.filename
        if (
            not name.strip()
            or name.strip(".") == ""
            or any(c in name for c in '/\\"')
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
        ):
            raise ValueError(f"filename must be a bare, non-empty name, got {name!r}")

    @property
    def mime(self) -> str:
        return MEDIA_MIME[self.media]

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        """Identity for the ledger. The bytes themselves are never stored."""
        return hashlib.sha256(self.data).hexdigest()
