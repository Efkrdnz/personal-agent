"""The Gemini Live session, multi-instantiable by construction.

ONE CONNECTION PER OBJECT. Nothing in this package is a singleton, a module
global or a class attribute that survives an instance, and that is a structural
decision rather than a stylistic one: the reference build's single
``self.session`` is what makes "place a Turkish call while the desk
conversation is open" impossible there, because changing voice means
reconnecting and its reconnect deliberately throws the resumption handle away.
Here a second leg is a second :class:`~jarvis.live.session.LiveSession` with its
own profile, its own source and sink, its own tool surface and its own handle,
and the only thing they share is a :class:`~jarvis.live.lease.LiveLease` that
says how many may be connected at once.

v1 runs exactly one. The lease capacity is a CONSTANT so that lifting the limit
is a config change rather than a refactor — the concurrent-session ceiling is
unverified and the sources conflict wildly (3 / 1000 / 5000), so the shipped
default is the pessimistic reading and spike S4 (``tools/probe_live.py``) is
what moves it.

IMPORT COST IS ZERO. ``google.genai`` is imported inside the functions that
build a config or open a socket, never at module scope, so this package imports
and its whole test suite runs with no API key, no network and no SDK — which is
also what makes the scripted transport in :mod:`jarvis.live.fake` able to test
reconnection, resumption and tool dispatch for real.
"""

from __future__ import annotations

#: The current model. NOT ``gemini-3.1-flash-live-preview``, which the build
#: sheet named and which is legacy; that pin is the single most expensive stale
#: fact in the sheet, because it fails at connect time with a message that
#: reads like an auth problem. Pin ``google-genai>=2.23,<3``.
MODEL = "gemini-3.8-live"

#: Raw little-endian int16 mono PCM, both directions. 16 kHz up, 24 kHz down —
#: not negotiable and not configurable per install, because every rate in the
#: audio graph was chosen so that nothing resamples on the desk path.
INPUT_RATE = 16_000
OUTPUT_RATE = 24_000

#: Seconds of audio per uplink chunk when a source does not say otherwise.
CHUNK_MS = 20

#: How long the local drop keeps discarding downlink audio when the server never
#: sends a turn boundary. There is NO way to tell the model to stop — the
#: AsyncSession has no ``interrupt()`` — so a barge-in is a local drop, and a
#: local drop with no expiry would leave the assistant permanently mute the
#: first time a boundary went missing.
DROP_TTL_S = 2.0

#: Context-window compression: mandatory for long calls, not an optimisation. A
#: 45-minute call ends in a hard session termination without it. The trigger is
#: deliberately well below the model's window so compression happens during a
#: pause rather than mid-sentence.
COMPRESSION_TRIGGER_TOKENS = 16_000
COMPRESSION_TARGET_TOKENS = 8_000

#: Reconnect backoff. Short, because a reconnect that replays a resumption
#: handle is nearly free and the user is waiting; capped, because a dead network
#: should not become a busy loop against someone's billing account.
BACKOFF_S = (0.25, 0.5, 1.0, 2.0, 4.0)
MAX_CONSECUTIVE_FAILURES = 5


def pcm_mime(rate: int) -> str:
    """The mime type for an uplink chunk. ALWAYS with the rate.

    A bare ``audio/pcm`` is a latent chipmunk bug: it works for exactly as long
    as there is one rate in the system, and the phone leg (8 kHz mu-law from the
    carrier, resampled locally to 16 kHz) is the second one. Tag the rate, never
    tag ``rate=8000`` and let the server upsample.
    """
    if rate <= 0:
        raise ValueError(f"sample rate must be positive, got {rate!r}")
    return f"audio/pcm;rate={rate}"


INPUT_MIME = pcm_mime(INPUT_RATE)

__all__ = [
    "BACKOFF_S",
    "CHUNK_MS",
    "COMPRESSION_TARGET_TOKENS",
    "COMPRESSION_TRIGGER_TOKENS",
    "DROP_TTL_S",
    "INPUT_MIME",
    "INPUT_RATE",
    "MAX_CONSECUTIVE_FAILURES",
    "MODEL",
    "OUTPUT_RATE",
    "pcm_mime",
]
