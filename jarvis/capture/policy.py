"""What may be captured, of what, and when not at all — as data, not as code.

"Never screenshot while a .env is focused" has to be a value somebody can read,
edit and test, not an ``if`` buried in a capture routine. So it is a frozen
dataclass with a default that already says no to the obvious disasters, and one
method that returns a :class:`~jarvis.capture.artifact.Refusal` or ``None``.

WHAT IS ACTUALLY CAPTURED, decided here and justified once.

``transcript`` is the DEFAULT and the recommended subject. It is the rendered
text of the conversation or the terminal scrollback: what the user almost always
means by "show me what it's doing", the only subject that can be redacted
exactly, the only one that stays legible on a phone screen, and the only one
that costs kilobytes instead of megabytes. It is also the only one that works
with no display server at all, which matters more than it sounds: the machine
this runs on may be headless, locked, or on Wayland.

``pane`` is the fallback: the window Claude Code is in. Chosen over the whole
screen because the interesting thing is almost never on the rest of the screen,
and because everything on the rest of the screen — a mail client, a password
manager, a second project's terminal — is blast radius with no upside.

``screen`` is NOT in the default allow-list. It is supported, because sometimes
the question really is "what is on my desk machine", but it has to be switched
on deliberately, by a person, in a policy object. A default that photographs
everything is a default that eventually photographs the wrong thing.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from jarvis.capture.artifact import Refusal, Subject

__all__ = ["DEFAULT_FORBIDDEN", "CapturePolicy", "Window", "text_only_policy"]

#: Substring or glob patterns matched case-insensitively against a window's
#: title, application name and document path. Substrings rather than regexes
#: because this list is meant to be edited by whoever gets burned next, at the
#: time they get burned, without looking anything up.
DEFAULT_FORBIDDEN: tuple[str, ...] = (
    ".env",
    ".envrc",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".p12",
    ".pgpass",
    ".npmrc",
    ".netrc",
    "authorized_keys",
    "credentials",
    "secrets",
    "keyring",
    "1password",
    "bitwarden",
    "keepassxc",
    "seed phrase",
)


@dataclass(frozen=True, slots=True)
class Window:
    """What the caller knows about what is on screen right now.

    Every field is optional and defaults to empty, because the caller's ability
    to answer "what is focused?" varies by platform and the policy must still be
    evaluable when it cannot. An empty :class:`Window` means "unknown", not
    "safe" — see :attr:`CapturePolicy.pixels_need_known_window`.
    """

    title: str = ""
    app: str = ""
    path: str = ""

    @property
    def known(self) -> bool:
        return bool(self.title or self.app or self.path)

    @property
    def haystack(self) -> str:
        return " ".join((self.title, self.app, self.path)).lower()


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    """The rules, as data. Construct one per process and pass it down.

    ``pixels_need_known_window`` is the interesting default. If the caller
    cannot tell us what is focused, the forbidden list cannot be evaluated at
    all, and a policy that silently permits the capture in that case is a policy
    that is switched off exactly when the desktop is least introspectable. So
    unknown blocks the PIXEL path and leaves the text path alone, which is the
    same asymmetry the rest of this package is built on.
    """

    text_first: bool = True
    allow_pixels: bool = True
    allowed_subjects: frozenset[Subject] = field(
        default_factory=lambda: frozenset({"transcript", "pane"})
    )
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN
    pixels_need_known_window: bool = True
    #: Refuse the pixel path outright when the caller already knows a shaped
    #: credential is visible and has named no region to cover it. Without OCR
    #: this is the ONLY moment the pixel path can be honest about a secret.
    refuse_pixels_on_visible_secret: bool = True
    mask_shapes: bool = True
    #: Telegram's document ceiling is 50 MB; stopping short of it means the
    #: refusal comes from here, with a sentence, rather than from a 413.
    max_bytes: int = 45_000_000

    def forbids(self, window: Window | None) -> str | None:
        """The first forbidden pattern this window matches, or ``None``."""
        if window is None or not window.known:
            return None
        hay = window.haystack
        for pattern in self.forbidden:
            low = pattern.lower()
            if "*" in low or "?" in low:
                if fnmatch.fnmatch(hay, f"*{low}*"):
                    return pattern
            elif low in hay:
                return pattern
        return None

    def check_subject(self, subject: Subject) -> Refusal | None:
        if subject in self.allowed_subjects:
            return None
        return Refusal(
            code="subject_not_allowed",
            detail=f"subject {subject!r} is not in {sorted(self.allowed_subjects)}",
            remedy=f"add {subject!r} to CapturePolicy.allowed_subjects if you meant it",
        )

    def check_window(self, window: Window | None) -> Refusal | None:
        """Applies to EVERY subject, including text.

        A transcript of a session that is editing a ``.env`` contains the
        ``.env``. Applying the never-capture list only to pixels would exempt
        the one path that carries the file's contents in full.
        """
        hit = self.forbids(window)
        if hit is None:
            return None
        return Refusal(
            code="policy_forbidden",
            detail=f"the focused window matches the never-capture pattern {hit!r}",
            remedy=f"close or unfocus it, or drop {hit!r} from CapturePolicy.forbidden",
        )

    def check_pixels(self, window: Window | None) -> Refusal | None:
        """Everything that gates the pixel path specifically."""
        if not self.allow_pixels:
            return Refusal(
                code="pixels_disabled",
                detail="CapturePolicy.allow_pixels is False",
                remedy="set allow_pixels=True if screenshots are wanted from this process",
            )
        if self.pixels_need_known_window and (window is None or not window.known):
            return Refusal(
                code="policy_forbidden",
                detail="nothing is known about the focused window, so the never-capture "
                "list cannot be evaluated",
                remedy=(
                    "pass a Window, or set pixels_need_known_window=False to accept "
                    "photographing an unidentified screen"
                ),
            )
        return None


def text_only_policy() -> CapturePolicy:
    """No pixels at all. A reasonable default for a channel that reaches a chat server."""
    return CapturePolicy(allow_pixels=False, allowed_subjects=frozenset({"transcript"}))
