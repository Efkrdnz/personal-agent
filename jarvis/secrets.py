"""Credentials, and the one place they are allowed to live.

THE RULE: no secret in the tree, no secret in config.toml. They go in the OS
keyring, and the environment is the documented fallback for a headless box that
has no keyring daemon.

``keyring`` is imported INSIDE the functions that need it, never at module
scope. That is deliberate and load-bearing: the spine must import under
``python -S`` on a machine with no third-party packages at all, and every
process opens this module. Its absence is not an error, it is a fallback to the
environment.

WHAT THIS MODULE WILL NOT DO. It will not print a secret, put one in an
exception message, or return one from ``__repr__``. :func:`probe` reports
PRESENCE and never value, because the natural next step after "which of my keys
are set?" is to paste the answer into a chat window.

THE HEADLESS TRAP, stated once so nobody rediscovers it: on Linux the login
keyring is unlocked by PAM at graphical login. A daemon started at boot, before
anyone logs in, has neither a session bus nor an unlocked keyring, and
``keyring`` will either raise or silently pick a useless backend. Jarvis is a
user service tied to a graphical session — it needs a microphone and speakers
anyway — so the condition it needs is one it already has. :func:`probe` says so
out loud rather than letting it degrade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "Secret",
    "SECRETS",
    "MissingSecret",
    "get",
    "require",
    "store",
    "forget",
    "probe",
    "Presence",
    "KEYRING_SERVICE",
]

KEYRING_SERVICE = "jarvis"

Source = Literal["keyring", "environment", "absent"]


class MissingSecret(RuntimeError):
    """A credential Jarvis needs is not set anywhere it looks."""

    def __init__(self, secret: Secret) -> None:
        super().__init__(
            f"{secret.name} is not set.\n"
            f"  what it is for : {secret.purpose}\n"
            f"  how to get one : {secret.how}\n"
            f"  store it       : python -m jarvis secrets set {secret.name}\n"
            f"  or export      : {secret.env}=..."
        )
        self.secret = secret


@dataclass(frozen=True, slots=True)
class Secret:
    """One credential: where it lives, what it unlocks, and how to obtain it.

    ``how`` exists because the answer to "Jarvis will not start" should be in the
    error message, not in a document the reader has to go and find.
    """

    name: str
    env: str
    purpose: str
    how: str
    #: False for credentials whose feature is optional; probe() reports those as
    #: "not set (feature off)" rather than as a problem.
    required: bool = False

    @property
    def keyring_user(self) -> str:
        return self.name


SECRETS: tuple[Secret, ...] = (
    Secret(
        name="gemini_api_key",
        env="JARVIS_GEMINI_API_KEY",
        purpose="the voice — Gemini Live is the conversational half of Jarvis",
        how="aistudio.google.com -> Get API key",
        required=True,
    ),
    Secret(
        name="github_token",
        env="JARVIS_GITHUB_TOKEN",
        purpose="creating the repo before a project starts, and reading new issues",
        how=(
            "github.com/settings/tokens -> fine-grained token. "
            "Contents: read/write, Administration: write if you want repo deletion "
            "to be possible at all (see docs/adr on undo)"
        ),
    ),
    Secret(
        name="telegram_bot_token",
        env="JARVIS_TELEGRAM_TOKEN",
        purpose="the remote channel: plan-mode buttons, screenshots, /status",
        how="message @BotFather on Telegram -> /newbot -> copy the token",
    ),
    Secret(
        name="google_oauth_client",
        env="JARVIS_GOOGLE_OAUTH_CLIENT",
        purpose="briefing sections 2 and 4 — Gmail and YouTube comments",
        how=(
            "console.cloud.google.com -> enable Gmail API and YouTube Data API v3 "
            "-> Credentials -> OAuth client ID (Desktop). Paste the client JSON."
        ),
    ),
)

_BY_NAME = {s.name: s for s in SECRETS}


def _lookup(name: str) -> Secret:
    try:
        return _BY_NAME[name]
    except KeyError:
        known = ", ".join(sorted(_BY_NAME))
        raise KeyError(f"unknown secret {name!r}; known: {known}") from None


def _from_keyring(secret: Secret) -> str | None:
    """The keyring, or None if it is absent, locked, or has no usable backend.

    Every failure mode here is "no keyring", not "no secret", so they collapse to
    the same answer and the caller falls through to the environment.
    """
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring.get_password(KEYRING_SERVICE, secret.keyring_user) or None
    except Exception:  # noqa: BLE001 — no package, no backend, locked, D-Bus absent
        return None


def get(name: str) -> str | None:
    """The credential, keyring first and environment second, or None."""
    secret = _lookup(name)
    return _from_keyring(secret) or os.environ.get(secret.env) or None


def require(name: str) -> str:
    """The credential, or :class:`MissingSecret` with instructions in the message."""
    value = get(name)
    if not value:
        raise MissingSecret(_lookup(name))
    return value


def store(name: str, value: str) -> None:
    """Put a credential in the keyring. Raises if there is no usable backend.

    Deliberately NOT falling back to writing a file: "I could not reach the
    keyring so I put your API key in ~/.jarvis/secrets.json" is precisely the
    behaviour this module exists to prevent.
    """
    secret = _lookup(name)
    if not value.strip():
        raise ValueError("refusing to store an empty value")
    import keyring  # type: ignore[import-not-found]

    keyring.set_password(KEYRING_SERVICE, secret.keyring_user, value)


def forget(name: str) -> bool:
    """Remove a credential from the keyring. True if one was there."""
    secret = _lookup(name)
    try:
        import keyring  # type: ignore[import-not-found]

        if keyring.get_password(KEYRING_SERVICE, secret.keyring_user) is None:
            return False
        keyring.delete_password(KEYRING_SERVICE, secret.keyring_user)
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True, slots=True)
class Presence:
    """Whether a credential is set, and where from. NEVER its value."""

    secret: Secret
    source: Source

    @property
    def ok(self) -> bool:
        return self.source != "absent"

    @property
    def blocking(self) -> bool:
        return self.secret.required and not self.ok

    @property
    def mark(self) -> str:
        """The status word alone, so a caller can align its own columns."""
        if self.ok:
            return "ok"
        return "MISSING" if self.secret.required else "not set"

    def detail(self) -> str:
        """Everything except the status word. Never the value."""
        if self.source == "keyring":
            return f"{self.secret.name}  (keyring)"
        if self.source == "environment":
            return f"{self.secret.name}  (environment — fine on a headless box)"
        if self.secret.required:
            return f"{self.secret.name}  — {self.secret.purpose}"
        return f"{self.secret.name}  — {self.secret.purpose} (that feature is off)"

    def line(self) -> str:
        return f"{self.mark:<7}  {self.detail()}"


def probe() -> tuple[Presence, ...]:
    """Which credentials are set and from where. Values are never read out."""
    out = []
    for secret in SECRETS:
        if _from_keyring(secret):
            source: Source = "keyring"
        elif os.environ.get(secret.env):
            source = "environment"
        else:
            source = "absent"
        out.append(Presence(secret=secret, source=source))
    return tuple(out)


def keyring_available() -> tuple[bool, str]:
    """Is there a usable keyring backend? Returns (ok, a sentence explaining).

    The sentence is for :func:`probe`'s report and names the headless case
    specifically, because "no recommended backend" is a message people search
    for and rarely find the actual cause of.
    """
    try:
        import keyring  # type: ignore[import-not-found]
        from keyring.backends import fail  # type: ignore[import-not-found]

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            return False, (
                "no usable keyring backend. On Linux the login keyring is unlocked by PAM at "
                "graphical login, so a service started before login has neither a session bus "
                "nor an unlocked keyring. Run Jarvis as a user service tied to your desktop "
                "session, or set the environment variables instead."
            )
        return True, f"keyring backend: {type(backend).__name__}"
    except ImportError:
        return False, "the keyring package is not installed (pip install -e '.[secrets]')"
    except Exception as e:  # noqa: BLE001
        return False, f"keyring is present but unusable: {type(e).__name__}"
