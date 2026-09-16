"""Screenshots and transcripts that can leave the machine safely.

Three questions, answered in three modules, in the order they have to be
answered in:

``backends``  Can a picture be taken here at all? On Wayland the honest answer
              is usually no, and it is returned as a typed refusal in two
              seconds rather than as a consent dialog nobody is there to click.
``redact``    Can the secrets be taken out? For text, exactly and provably. For
              pixels, only where the caller named a region — so the picture
              carries a warning band saying so.
``service``   Do it, record it in the effects ledger, and hand back bytes.

The delivery contract is :class:`Artifact`: bytes, a filename, a media kind. This
package does not know whether that goes to Telegram, to a phone leg or to a local
HUD, and must not learn — ``jarvis.telegram`` is deliberately not imported here.

Standard library only, like the spine. It imports on a machine with no display,
no Pillow and no bot token, which is exactly the machine its tests run on.
"""

from __future__ import annotations

from jarvis.capture.artifact import (
    MEDIA_MIME,
    Artifact,
    CaptureRefused,
    MediaKind,
    RedactionReport,
    Refusal,
    RefusalCode,
    Subject,
)
from jarvis.capture.backends import (
    CAPTURE_TOOLS,
    WAYLAND_REFUSAL,
    BackendStatus,
    Capturer,
    CommandCapturer,
    NullCapturer,
    SyntheticCapturer,
    Tool,
    choose_capturer,
    detect_backend,
)
from jarvis.capture.png import Box, Canvas, RawImage, UnsupportedPNG, decode_png, encode_png
from jarvis.capture.policy import DEFAULT_FORBIDDEN, CapturePolicy, Window, text_only_policy
from jarvis.capture.redact import (
    REDACTION_FILL,
    SECRET_SHAPES,
    Finding,
    banner,
    redact_pixels,
    redact_text,
    scan_shapes,
    surviving_secrets,
)
from jarvis.capture.service import (
    CAPTURE_REVERSIBILITY,
    PIXEL_WARNING,
    capture,
    capture_pixels,
    capture_transcript,
    record_capture,
    render_transcript,
)

__all__ = [
    "CAPTURE_REVERSIBILITY",
    "CAPTURE_TOOLS",
    "DEFAULT_FORBIDDEN",
    "MEDIA_MIME",
    "PIXEL_WARNING",
    "REDACTION_FILL",
    "SECRET_SHAPES",
    "WAYLAND_REFUSAL",
    "Artifact",
    "BackendStatus",
    "Box",
    "Canvas",
    "CapturePolicy",
    "CaptureRefused",
    "Capturer",
    "CommandCapturer",
    "Finding",
    "MediaKind",
    "NullCapturer",
    "RawImage",
    "RedactionReport",
    "Refusal",
    "RefusalCode",
    "Subject",
    "SyntheticCapturer",
    "Tool",
    "UnsupportedPNG",
    "Window",
    "banner",
    "capture",
    "capture_pixels",
    "capture_transcript",
    "choose_capturer",
    "decode_png",
    "detect_backend",
    "encode_png",
    "record_capture",
    "redact_pixels",
    "redact_text",
    "render_transcript",
    "scan_shapes",
    "surviving_secrets",
    "text_only_policy",
]
