"""Capture, redact, record, hand back bytes. The order is the design.

ONE ENTRY POINT, TWO PATHS, AND THE HONEST ONE IS THE DEFAULT. :func:`capture`
prefers text whenever text is available, because text is the only thing that can
be redacted exactly and proved clean afterwards. The pixel path exists, works,
and is treated throughout as the fallback it is: it carries a
:class:`~jarvis.capture.artifact.RedactionReport` whose ``exact`` is False, a
caption that says so, and a warning band burned into the picture itself.

NOTHING LEAVES WITHOUT A LEDGER ROW. Every capture is recorded through
:mod:`jarvis.effects` before the bytes are returned, so "what have you sent of my
screen?" is a SELECT rather than a memory. The class is ``compensatable`` and the
row carries NO undo plan, which is a deliberate pair of decisions:

* not ``reversible``, because the only reason these bytes exist is to leave the
  machine, and :func:`jarvis.effects.spoken_effect_line` would generate "I can
  put that back exactly as it was" — a sentence that is meaningless for a
  screenshot and false the moment a channel has sent one;
* no plan, because deleting a sent message is the CHANNEL's compensation, on the
  channel's 48-hour window, recorded as the channel's own
  ``telegram.send_document`` effect. A plan here would be this package promising
  something it has no way to perform, which is precisely what
  :mod:`jarvis.effects` refuses to let anyone spell.

The resulting spoken line is "…I can't reverse that…there is nothing I can act
on", which is exactly right for a capture that this package cannot unsend.

WHAT THIS MODULE MUST NOT KNOW. Not the channel, not the chat id, not the bot
token. :class:`~jarvis.capture.artifact.Artifact` is bytes, a filename and a
kind; the Telegram leg, the phone leg and the HUD each decide for themselves
what to do with it. That is why ``jarvis.telegram`` is not imported here and
must not become imported here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from jarvis.bus import Redactor
from jarvis.capture.artifact import (
    Artifact,
    CaptureRefused,
    RedactionReport,
    Refusal,
    Subject,
)
from jarvis.capture.backends import Capturer, NullCapturer
from jarvis.capture.png import Box, encode_png
from jarvis.capture.policy import CapturePolicy, Window
from jarvis.capture.redact import banner, redact_pixels, redact_text, surviving_secrets
from jarvis.effects import Effect, Reversibility, record_effect
from jarvis.ids import now

__all__ = [
    "CAPTURE_REVERSIBILITY",
    "PIXEL_WARNING",
    "capture",
    "capture_pixels",
    "capture_transcript",
    "record_capture",
    "render_transcript",
]

#: The classification, decided here and BEFORE anything is captured, exactly as
#: :mod:`jarvis.effects` requires. It lives in this module rather than in
#: ``effects.KIND_REVERSIBILITY`` only because the spine is closed to edits from
#: a channel stage; the right long-term home for these two lines is that table,
#: so that :func:`jarvis.effects.reversibility_of` can answer for them too.
CAPTURE_REVERSIBILITY: dict[str, Reversibility] = {
    "capture.transcript": "compensatable",
    "capture.screenshot": "compensatable",
}

#: Burned into the picture and repeated in the caption. Uppercase because the
#: 5x7 font has no lowercase, and short because it has to fit across a phone.
PIXEL_WARNING: tuple[str, ...] = ("NOT TEXT-REDACTED", "SECRETS MAY BE VISIBLE")

_TRIM_MARKER = "[earlier output trimmed to fit]\n"


def capture(
    con: sqlite3.Connection,
    *,
    redactor: Redactor,
    subject: Subject = "transcript",
    text: str | None = None,
    capturer: Capturer | None = None,
    window: Window | None = None,
    window_id: str | None = None,
    boxes: Sequence[Box] = (),
    policy: CapturePolicy | None = None,
    caption: str = "",
    job_id: str | None = None,
    actor: str = "system",
    now_ts: str | None = None,
    record: bool = True,
) -> Artifact:
    """Get the best artifact this machine can honestly produce, or refuse.

    Text wins whenever there is text and the policy has not been told otherwise.
    That is the whole "make the honest thing the easy thing" rule, expressed as
    a default rather than as advice: a caller that hands over both a transcript
    and a capturer gets the transcript, and has to say ``text_first=False`` to
    get the picture instead.
    """
    pol = policy or CapturePolicy()
    if text is not None and (subject == "transcript" or pol.text_first):
        return capture_transcript(
            con,
            text=text,
            redactor=redactor,
            window=window,
            policy=pol,
            caption=caption,
            job_id=job_id,
            actor=actor,
            now_ts=now_ts,
            record=record,
        )
    if subject == "transcript":
        raise ValueError(
            "the transcript subject needs text; pass text=… or ask for 'pane' or 'screen'"
        )
    return capture_pixels(
        con,
        capturer=capturer or NullCapturer(),
        redactor=redactor,
        subject=subject,
        window=window,
        window_id=window_id,
        boxes=boxes,
        visible_text=text,
        policy=pol,
        caption=caption,
        job_id=job_id,
        actor=actor,
        now_ts=now_ts,
        record=record,
    )


def capture_transcript(
    con: sqlite3.Connection,
    *,
    text: str,
    redactor: Redactor,
    window: Window | None = None,
    policy: CapturePolicy | None = None,
    caption: str = "",
    job_id: str | None = None,
    actor: str = "system",
    now_ts: str | None = None,
    record: bool = True,
) -> Artifact:
    """The good path: redact by string replacement, then prove it worked."""
    pol = policy or CapturePolicy()
    ts = now_ts or now()
    if not text:
        # Caller's bug, in the same class as asking for a transcript with no
        # text at all, and said here rather than three frames down as "an empty
        # artifact has nothing to deliver".
        raise ValueError("there is no transcript to send; capture_transcript needs non-empty text")
    _refuse_if(pol.check_subject("transcript"))
    _refuse_if(pol.check_window(window))

    clean, report = redact_text(text, redactor=redactor, mask_shapes=pol.mask_shapes)
    body, trimmed = _fit(clean, pol.max_bytes)

    # Second look, after trimming, because the trim is the last thing that
    # touches these bytes and a gate that runs before the last mutation is not
    # a gate. Cheap, and it is the assertion the text-first argument rests on.
    if surviving_secrets(body, redactor):
        raise CaptureRefused(
            Refusal(
                code="secret_visible",
                detail="a supplied secret literal survived redaction of the transcript",
                remedy="this is a bug in jarvis.capture.redact, not a condition to retry",
            )
        )

    artifact = Artifact(
        data=body.encode("utf-8"),
        filename=f"jarvis-transcript-{_stamp(ts)}.txt",
        media="text",
        subject="transcript",
        caption=caption or _text_caption(report),
        redaction=report,
        warnings=("Trimmed: only the most recent output is here.",) if trimmed else (),
    )
    return _finish(con, artifact, "capture.transcript", redactor, window, job_id, actor, record)


def capture_pixels(
    con: sqlite3.Connection,
    *,
    capturer: Capturer,
    redactor: Redactor,
    subject: Subject = "pane",
    window: Window | None = None,
    window_id: str | None = None,
    boxes: Sequence[Box] = (),
    visible_text: str | None = None,
    policy: CapturePolicy | None = None,
    caption: str = "",
    job_id: str | None = None,
    actor: str = "system",
    now_ts: str | None = None,
    record: bool = True,
) -> Artifact:
    """The fallback: photograph, cover the named regions, warn about the rest.

    ``visible_text`` is the caller's best knowledge of what is legible on that
    screen — typically the same scrollback it would have sent as a transcript.
    It is the ONLY thing that lets the pixel path say anything at all about
    secrets, so when it contains one and no region was named to cover it, this
    refuses. Without OCR there is no other moment at which that check can happen.
    """
    pol = policy or CapturePolicy()
    ts = now_ts or now()
    _refuse_if(pol.check_subject(subject))
    _refuse_if(pol.check_window(window))
    _refuse_if(pol.check_pixels(window))

    # A box that covers no pixel is not a redaction. Counting `boxes` truthily
    # would let Box(0, 0, 0, 0) — or one whose coordinates fell off the image —
    # lift the refusal below and send the credential out in the pixels.
    covering = tuple(b for b in boxes if b.area)
    secret_on_screen = False
    if visible_text is not None and pol.refuse_pixels_on_visible_secret:
        _, probe = redact_text(visible_text, redactor=redactor, mask_shapes=pol.mask_shapes)
        secret_on_screen = probe.anything_removed
        if secret_on_screen and not covering:
            raise CaptureRefused(
                Refusal(
                    code="secret_visible",
                    detail=(
                        f"{probe.literals_removed} literal and {len(probe.shapes_removed)} "
                        "shaped credential(s) are legible on that screen and no region was "
                        "named to cover them"
                    ),
                    remedy="send the transcript instead, or pass boxes=… covering the region",
                )
            )

    img = capturer.grab(subject, window_id=window_id)
    redacted, report = redact_pixels(img, tuple(boxes))
    # The box was checked against the caller's arithmetic above; this checks it
    # against the picture that was actually taken, which is the only version
    # that matters and is not known until after the grab.
    if secret_on_screen and not report.pixels_filled:
        raise CaptureRefused(
            Refusal(
                code="secret_visible",
                detail=(
                    "a credential is legible on that screen and every named region fell "
                    "outside the captured image, so nothing was covered"
                ),
                remedy="send the transcript instead, or pass boxes=… inside the captured frame",
            )
        )
    stamped = banner(redacted, PIXEL_WARNING)
    data = encode_png(stamped)
    if len(data) > pol.max_bytes:
        # Unlike text, a picture cannot be trimmed without changing what it
        # shows, so this is a refusal rather than a truncation.
        raise CaptureRefused(
            Refusal(
                code="too_large",
                detail=f"the PNG is {len(data)} bytes, over the {pol.max_bytes} limit",
                remedy="capture the pane rather than the whole screen, or send the transcript",
            )
        )

    artifact = Artifact(
        data=data,
        filename=f"jarvis-{subject}-{_stamp(ts)}.png",
        media="image",
        subject=subject,
        caption=caption or _pixel_caption(report),
        redaction=report,
        warnings=(
            "This picture was NOT text-redacted. Anything legible in it left the machine as it "
            "was on screen.",
        ),
    )
    return _finish(con, artifact, "capture.screenshot", redactor, window, job_id, actor, record)


def render_transcript(
    lines: Sequence[str] | str,
    *,
    title: str = "",
    now_ts: str | None = None,
) -> str:
    """Plain text with a two-line header. No markup, no escaping, no surprises.

    Deliberately not Markdown: this goes to a chat client that will try to
    interpret backticks and underscores in a terminal dump, and a transcript
    that renders as bold nonsense is a transcript nobody can read. The channel
    may wrap it in a code fence if it wants to; that is its decision, not this
    one.
    """
    body = lines if isinstance(lines, str) else "\n".join(lines)
    head = f"{title or 'Jarvis transcript'} — {now_ts or now()}"
    return f"{head}\n{'-' * len(head)}\n{body.rstrip()}\n"


def record_capture(
    con: sqlite3.Connection,
    artifact: Artifact,
    *,
    kind: str,
    redactor: Redactor,
    window: Window | None = None,
    job_id: str | None = None,
    actor: str = "system",
) -> Effect:
    """Append the ledger row. Compensatable, no plan — see the module docstring.

    The summary goes through the redactor too. It names what was captured, and
    what was captured may be a window whose title is a path to a file whose name
    is a secret; a ledger row is not a safe place for that any more than a chat
    message is.
    """
    summary, _ = redact_text(_summary(artifact, window), redactor=redactor, mask_shapes=True)
    return record_effect(
        con,
        kind=kind,
        summary=summary,
        reversibility=CAPTURE_REVERSIBILITY[kind],
        job_id=job_id,
        provider_ref={
            "sha256": artifact.sha256,
            "bytes": artifact.size,
            "filename": artifact.filename,
            "media": artifact.media,
            "subject": artifact.subject,
            "exact_redaction": artifact.redaction.exact,
            "method": artifact.redaction.method,
            "literals_removed": artifact.redaction.literals_removed,
            "shapes_removed": list(artifact.redaction.shapes_removed),
            "regions_filled": artifact.redaction.regions_filled,
        },
        actor=actor,
    )


# ───────────────────────────── internals ─────────────────────────────


def _finish(
    con: sqlite3.Connection,
    artifact: Artifact,
    kind: str,
    redactor: Redactor,
    window: Window | None,
    job_id: str | None,
    actor: str,
    record: bool,
) -> Artifact:
    if not record:
        return artifact
    effect = record_capture(
        con,
        artifact,
        kind=kind,
        redactor=redactor,
        window=window,
        job_id=job_id,
        actor=actor,
    )
    # dataclasses.replace would do, but spelling the construction out keeps the
    # frozen Artifact the single place that validates a filename.
    return Artifact(
        data=artifact.data,
        filename=artifact.filename,
        media=artifact.media,
        subject=artifact.subject,
        caption=artifact.caption,
        redaction=artifact.redaction,
        lossless=artifact.lossless,
        warnings=artifact.warnings,
        effect_id=effect.id,
    )


def _refuse_if(refusal: Refusal | None) -> None:
    if refusal is not None:
        raise CaptureRefused(refusal)


def _summary(artifact: Artifact, window: Window | None) -> str:
    """Past tense, spoken, and it may not claim more than was done."""
    what = {
        "transcript": "the transcript",
        "pane": "a screenshot of the Claude Code pane",
        "screen": "a screenshot of the whole screen",
    }[artifact.subject]
    where = f" of {window.title}" if window is not None and window.title else ""
    size = _kb(artifact.size)
    if artifact.redaction.exact:
        removed = artifact.redaction.literals_removed + len(artifact.redaction.shapes_removed)
        cleaned = (
            f" and took {removed} credential{'s' if removed != 1 else ''} out of it by string "
            "replacement"
            if removed
            else " and found no credentials in it"
        )
        return f"I captured {what}{where} ({size}){cleaned}."
    covered = (
        f", covered {artifact.redaction.regions_filled} region"
        f"{'s' if artifact.redaction.regions_filled != 1 else ''}"
        if artifact.redaction.regions_filled
        else ""
    )
    return (
        f"I captured {what}{where} ({size}){covered}, and I could not check the pixels "
        "for credentials."
    )


def _text_caption(report: RedactionReport) -> str:
    if not report.anything_removed:
        return "Transcript. No credentials found in it."
    bits: list[str] = []
    if report.literals_removed:
        bits.append(f"{report.literals_removed} known secret(s)")
    if report.shapes_removed:
        bits.append(", ".join(sorted(set(report.shapes_removed))))
    return f"Transcript, redacted by exact string replacement: {'; '.join(bits)}."


def _pixel_caption(report: RedactionReport) -> str:
    covered = (
        f"{report.regions_filled} region(s) covered."
        if report.regions_filled
        else "No regions were covered."
    )
    return (
        f"Screenshot - NOT text-redacted. {covered} "
        "Anything legible here left the machine as it was on screen."
    )


def _fit(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the TAIL. The recent end of a transcript is the part anyone wanted."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    budget = max(0, max_bytes - len(_TRIM_MARKER.encode("utf-8")))
    tail = data[-budget:] if budget else b""
    # A byte slice can land mid-character; walk past any continuation bytes
    # rather than decoding with errors='ignore', which would silently mangle
    # the first line instead of dropping it.
    i = 0
    while i < len(tail) and (tail[i] & 0xC0) == 0x80:
        i += 1
    return _TRIM_MARKER + tail[i:].decode("utf-8"), True


def _kb(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} kB"


def _stamp(ts: str) -> str:
    """``2026-09-16T12:00:00.000Z`` -> ``20260916T120000Z``, safe on every filesystem.

    The Z is appended rather than kept, because a caller may hand over a
    timestamp with no fractional part and stripping first means one Z either way.
    """
    kept = "".join(c for c in ts.split(".")[0] if c in "0123456789T")
    return f"{kept}Z"
