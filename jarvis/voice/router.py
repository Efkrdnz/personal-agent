"""Every sound this system makes starts as an :class:`Utterance` in a queue here.

THE RULE, and it is a TYPE rather than a convention: LOAD-BEARING TEXT NEVER
PASSES THROUGH A GENERATIVE MODEL ON ITS WAY TO THE USER. Gemini Live has no
verbatim path — everything it "says" is a paraphrase of an injected fake user
turn — so a second, deterministic reader exists beside it, and the decision of
which one speaks a given string is made HERE, from the string's own fidelity
tier, not by whoever happened to be holding a session object.

Three tiers, because scoping is what makes the reader cheap:

``exact``
    Option labels and the ordinal that binds them, confirmed requirement items,
    the chosen-option read-back, the AI-disclosure line. Answer keys and
    contract text: one wrong word silently builds the wrong thing. A full
    four-question plan round is about sixteen labels — a few hundred bytes.
``faithful``
    The question text, the ``ExitPlanMode`` plan body, quoted paths and
    commands. The user must understand them; nothing is matched against them.
``free``
    "Claude Code has a question about storage", progress narration, briefing
    prose. Paraphrase is not merely tolerable here, it is better.

There is no ``speak()`` on this class and there is deliberately no way to hand a
bare string to a track. Callers enqueue an :class:`Utterance`, whose fidelity is
a required part of its identity, and the router picks the track. Routing
``exact`` to the Gemini track raises :class:`FidelityViolation` — and
``jarvis.audio`` raises the same invariant again at the output bus, because two
independent checks is the right amount for the one bug this whole design exists
to prevent: a silent downgrade to paraphrase, which produces audio that sounds
perfect and is wrong.

When the reader is dead the system REFUSES. It does not fall back to the
paraphrase channel, ever. With a text channel attached it announces the failure
in the Live voice and asks for the numbers off the screen; with no text channel
(a phone leg) it says so in different words — there is no screen to point at —
and the question DEFERS. Both outcomes are recorded as a :class:`Refused` so the
caller can see what happened to its text.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from jarvis.bus import Redactor, publish

__all__ = [
    "DEFERRAL_LINES",
    "EARCON_MARKS",
    "FIDELITIES",
    "LIVE_TRACK",
    "REFUSAL_LINES",
    "VERBATIM_TRACK",
    "Delivery",
    "EarconMark",
    "Fidelity",
    "FidelityViolation",
    "LiveSink",
    "Mirror",
    "NoReader",
    "OutputRouter",
    "PcmSink",
    "Refused",
    "Spoken",
    "Track",
    "TrackSink",
    "Utterance",
    "VerbatimSink",
    "assert_routable",
    "publish_said",
    "refusal_line",
    "track_for",
]

#: The tiers, in descending order of how much a wrong word costs.
Fidelity = Literal["exact", "faithful", "free"]
FIDELITIES: frozenset[str] = frozenset({"exact", "faithful", "free"})

Track = Literal["verbatim", "live"]
VERBATIM_TRACK: Track = "verbatim"
LIVE_TRACK: Track = "live"

#: A 150 ms earcon brackets every verbatim EPISODE, not every clip: the boundary
#: the user learns is "somebody else's exact words start here", and one beep per
#: option would teach them nothing and sound like a fire alarm.
EarconMark = Literal["none", "open", "close", "both"]
EARCON_MARKS: frozenset[str] = frozenset({"none", "open", "close", "both"})

#: Spoken in the LIVE voice, so paraphrase is fine and a wrong word costs
#: nothing. What matters is that it says the reader is broken rather than
#: quietly reading the options in the voice that cannot promise them.
REFUSAL_LINES: dict[str, str] = {
    "en": (
        "My reader voice isn't working, so I can't read you the options exactly. "
        "They're on screen and I've sent them to Telegram — tell me the numbers."
    ),
    "tr": (
        "Okuyucu sesim çalışmıyor, bu yüzden seçenekleri birebir okuyamıyorum. "
        "Ekranda ve Telegram'da duruyorlar — bana numaraları söyle."
    ),
}

#: The SAME failure on a leg with no screen and no Telegram binding. It needs its
#: own words rather than the line above, because "they're on screen" spoken down
#: a phone line is a false statement, and a layer whose entire purpose is that
#: the user can trust what they hear cannot afford one. The question defers; say
#: that it defers.
DEFERRAL_LINES: dict[str, str] = {
    "en": (
        "My reader voice isn't working, so I can't read you the options exactly, "
        "and there's no screen on this line. I'm holding the question until I can "
        "read it to you properly."
    ),
    "tr": (
        "Okuyucu sesim çalışmıyor, bu yüzden seçenekleri birebir okuyamıyorum ve "
        "bu hatta ekran yok. Sana düzgün okuyabilene kadar soruyu bekletiyorum."
    ),
}


class NoReader(RuntimeError):
    """A track this router was asked to use is not attached.

    Its own type so the refusal path can tell "the engines all failed" from "the
    leg was never wired up", which are the same sound and very different bugs.
    """


class FidelityViolation(RuntimeError):
    """Load-bearing text was aimed at a channel that cannot promise it.

    Raised rather than logged. A downgrade here is inaudible to the user and
    produces a build that is confidently wrong, which is worse than silence.
    """


@dataclass(frozen=True, slots=True)
class Utterance:
    """One thing to say, carrying the tier that decides who says it.

    ``label`` is the load-bearing substring of ``text`` — on an option clip the
    text is ``"2. Postgres"`` and the label is ``"Postgres"`` — so a checker can
    compare the answer key byte for byte while the reader still speaks the
    ordinal and the label as ONE clip. The adjacency is the binding; a comma or
    an "option" between them is what ``tools/fidelity_probe.py`` scores as a
    miss, and it is also what makes "the second one" resolve against nothing.

    ``track`` pins an utterance to a track regardless of its tier. It exists for
    the one shape that needs it: during a read-options episode the framing and
    the tail are ``free`` text but are read by the READER, because switching
    voices mid-episode is what makes the seam jarring instead of legible.
    Pinning to the verbatim track is always safe; pinning ``exact`` text to the
    live track raises, exactly as routing it there implicitly would.
    """

    text: str
    fidelity: Fidelity = "free"
    lang: str = "en"
    tag: str = ""
    index: int | None = None
    label: str | None = None
    track: Track | None = None
    earcon: EarconMark = "none"
    mirror: bool = True
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("an Utterance with no text is a bug at the caller, not silence")
        if self.fidelity not in FIDELITIES:
            raise ValueError(f"unknown fidelity {self.fidelity!r}; one of {sorted(FIDELITIES)}")
        if not self.lang.strip():
            raise ValueError("lang must be set; the engine ladder picks a voice from it")
        if self.earcon not in EARCON_MARKS:
            raise ValueError(f"unknown earcon mark {self.earcon!r}")
        if self.track is not None and self.track not in (VERBATIM_TRACK, LIVE_TRACK):
            raise ValueError(f"unknown track {self.track!r}")
        if self.index is not None and self.index < 1:
            raise ValueError("option indices are 1-based; the user hears them")
        if self.label is not None:
            if not self.label.strip():
                raise ValueError("an empty label cannot be an answer key")
            if self.label not in self.text:
                # The clip that gets spoken is `text`. If the answer key is not
                # inside it, the mirror, the probe and the user are looking at
                # three different strings.
                raise ValueError(f"label {self.label!r} is not a substring of {self.text!r}")
        if self.fidelity == "exact" and not self.mirror:
            # Mirroring is additive, never a substitute — but it is also the
            # user's only way to CHECK what they heard, and it is what the
            # refusal path points at when the reader dies.
            raise ValueError("exact-tier text is always mirrored; mirror=False is not an option")

    @property
    def load_bearing(self) -> bool:
        return self.fidelity != "free"


@runtime_checkable
class PcmSink(Protocol):
    """Somewhere PCM16 mono 24 kHz bytes can be written. Usually an AudioBus track.

    ``tier`` travels with the bytes so the audio layer can apply the SAME rule to
    audio that this layer applies to text. Without it the mixer would be looking
    at an anonymous block of samples and could only trust that whoever produced
    it did the right thing, which is precisely the trust this design refuses.
    """

    async def write(self, pcm: bytes, *, tier: Fidelity = "free") -> None: ...


@dataclass(frozen=True, slots=True)
class TrackSink:
    """The seam onto a ``jarvis.audio`` mixer track. Bytes in, samples out.

    THE SECOND OF THE TWO CHECKS. The router refuses to ROUTE exact text to the
    paraphrase track; the mixer refuses to ACCEPT exact audio on a track that
    cannot carry it. Neither subsumes the other: the router also guards text
    that never becomes audio at all (a Telegram render, a phone leg with no
    reader), and the mixer also guards audio that reached a track by some route
    the router never saw. Two layers, because the failure is inaudible.

    numpy is imported inside the call, not at module scope, so the voice layer
    keeps importing on a machine with no audio stack whatsoever.
    """

    track: Any

    async def write(self, pcm: bytes, *, tier: Fidelity = "free") -> None:
        import numpy as np

        # frombuffer is a read-only view over bytes we do not own; the mixer
        # queues what it is given, so it gets a copy it may treat as its own.
        self.track.write(np.frombuffer(pcm, dtype="<i2").copy(), tier=tier)


@runtime_checkable
class VerbatimSink(Protocol):
    """The deterministic reader. Returns the number of bytes it put on the wire."""

    async def speak(self, utt: Utterance) -> int: ...


@runtime_checkable
class LiveSink(Protocol):
    """The conversational voice. It PARAPHRASES, which is why it never sees exact text."""

    async def say(self, utt: Utterance) -> None: ...


class Mirror(Protocol):
    """Text out to the TUI, Telegram and the activity log.

    ``track`` is ``None`` when the text was not spoken at all, which is the case
    the refusal path depends on: the options reach the screen even though no
    voice could read them.
    """

    def __call__(self, utt: Utterance, track: Track | None) -> None: ...


@dataclass(frozen=True, slots=True)
class Spoken:
    utterance: Utterance
    track: Track
    nbytes: int = 0


@dataclass(frozen=True, slots=True)
class Refused:
    utterance: Utterance
    reason: str
    action: Literal["announced", "deferred"]


Delivery = Spoken | Refused


def track_for(fidelity: Fidelity, *, paraphrase_ok: bool = False) -> Track:
    """Which voice may carry this tier.

    ``paraphrase_ok`` is a per-leg policy for FAITHFUL text only — a dropped
    adjective in a ninety-second plan body is cosmetic, and Gemini reads long
    prose better than any local engine. It cannot reach ``exact``: that branch
    is not written, rather than written and guarded.
    """
    if fidelity == "exact":
        return VERBATIM_TRACK
    if fidelity == "faithful":
        return LIVE_TRACK if paraphrase_ok else VERBATIM_TRACK
    return LIVE_TRACK


def assert_routable(utt: Utterance, track: Track) -> None:
    """The invariant, checked here and again at the audio bus."""
    if utt.fidelity == "exact" and track == LIVE_TRACK:
        raise FidelityViolation(
            f"exact-tier text {utt.text!r} was routed to the {LIVE_TRACK} track, "
            "which paraphrases everything it says"
        )


def refusal_line(lang: str, *, has_text_channel: bool = True) -> str:
    """What Jarvis says when the reader is dead. Paraphrase is fine; silence is not.

    ``has_text_channel`` picks between "read them off the screen" and "I'm
    holding the question", which are different promises. Telling a phone caller
    to look at a screen is worse than saying nothing: it sounds like a recovery
    and leaves them waiting for options that were never sent anywhere.
    """
    table = REFUSAL_LINES if has_text_channel else DEFERRAL_LINES
    return table.get(lang.split("-")[0].lower(), table["en"])


def publish_said(
    con: sqlite3.Connection,
    actor: str,
    utt: Utterance,
    track: Track | None,
    *,
    job_id: str | None = None,
    channel_id: str | None = None,
    redactor: Redactor | None = None,
    idem_key: str | None = None,
) -> str:
    """Mirror one utterance into the activity log. Connection first, as always.

    The router itself never holds a connection: it runs inside whichever process
    owns the speakers, and that process may not be the one that opened the
    database. Wiring is ``router.mirror = partial(publish_said, con, "voice")``
    at the call site, where "which connection" has an answer you can read off.
    """
    payload: dict[str, Any] = {
        "text": utt.text,
        "fidelity": utt.fidelity,
        "lang": utt.lang,
        "track": track,
        "spoken": track is not None,
    }
    if utt.tag:
        payload["tag"] = utt.tag
    if utt.index is not None:
        payload["index"] = utt.index
    if utt.label is not None:
        payload["label"] = utt.label
    return publish(
        con,
        "speech.said",
        actor,
        payload,
        job_id=job_id,
        request_id=utt.request_id,
        channel_id=channel_id,
        redactor=redactor,
        idem_key=idem_key,
    )


@dataclass(slots=True)
class _Episode:
    """What one :meth:`OutputRouter.drain` has already done about a failure.

    Per-drain, not per-router: the apology is once per episode, and an episode is
    exactly one drain. ``apologies`` holds IDENTITIES rather than tags because a
    tag is caller-supplied and "is this the clip I just manufactured?" must not
    be answerable by a caller that happens to pick the same string.
    """

    announced: set[str] = field(default_factory=set)
    apologies: list[Utterance] = field(default_factory=list)

    def manufactured(self, utt: Utterance) -> bool:
        return any(a is utt for a in self.apologies)


@dataclass
class OutputRouter:
    """The queue and the routing decision. Multi-instantiable; one per leg.

    No module-level state and no singleton: a desk leg and a phone leg each own
    a router with their own sinks, which is the whole reason stage 6 is a
    re-wiring rather than a rewrite.
    """

    verbatim: VerbatimSink | None = None
    live: LiveSink | None = None
    mirror: Mirror | None = None
    has_text_channel: bool = False
    paraphrase_faithful: bool = False
    on_defer: Callable[[Utterance, str], None] | None = None
    _queue: deque[Utterance] = field(default_factory=deque, repr=False)

    def enqueue(self, utt: Utterance) -> None:
        """Queue one utterance. This is the ONLY way anything makes a sound.

        The routing check runs here as well as at dispatch, so a caller that
        aims exact text at the live track finds out at the line that did it
        rather than inside an audio callback three seconds later.
        """
        assert_routable(utt, self._track_for(utt))
        self._queue.append(utt)

    def enqueue_all(self, utts: Iterable[Utterance]) -> None:
        for utt in utts:
            self.enqueue(utt)

    @property
    def pending(self) -> int:
        return len(self._queue)

    def _track_for(self, utt: Utterance) -> Track:
        if utt.track is not None:
            return utt.track
        return track_for(utt.fidelity, paraphrase_ok=self.paraphrase_faithful)

    async def drain(self) -> tuple[Delivery, ...]:
        """Speak everything queued, in order, and say what happened to each clip.

        Returns rather than logs, because the caller — the read-options episode,
        the read-back, the briefing — is the only thing that knows what a
        refusal means for ITS turn.
        """
        out: list[Delivery] = []
        episode = _Episode()
        while self._queue:
            utt = self._queue.popleft()
            out.append(await self._deliver(utt, episode))
        return tuple(out)

    async def _deliver(self, utt: Utterance, episode: _Episode) -> Delivery:
        track = self._track_for(utt)
        assert_routable(utt, track)
        try:
            if track == VERBATIM_TRACK:
                if self.verbatim is None:
                    raise NoReader("no verbatim sink is attached to this router")
                nbytes = await self.verbatim.speak(utt)
            else:
                if self.live is None:
                    raise NoReader("no live sink is attached to this router")
                await self.live.say(utt)
                nbytes = 0
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad: every engine in the ladder has already been
            # tried by the time a VerbatimSpeaker gives up, and the ways a
            # session drops mid-turn are not enumerable from here. What matters
            # is that NO exception can end with the paraphrase channel quietly
            # reading an answer key, so everything lands in the refusal path.
            #
            # Ordinary free-tier prose is the one exception: a narration clip
            # the live session dropped is a session problem, not a fidelity one,
            # and the caller should see it. An apology THIS ROUTER manufactured
            # is not ordinary prose — if it could raise, the refusal path would
            # be abortable by the thing it just queued, and the load-bearing
            # clips still in the queue would be dropped unmirrored and
            # undeferred: exactly the silent loss the refusal exists to prevent.
            if utt.fidelity == "free" and track == LIVE_TRACK and not episode.manufactured(utt):
                raise
            return self._refuse(utt, f"{type(exc).__name__}: {exc}", episode)
        if self.mirror is not None and utt.mirror:
            self.mirror(utt, track)
        return Spoken(utterance=utt, track=track, nbytes=nbytes)

    def _refuse(self, utt: Utterance, reason: str, episode: _Episode) -> Refused:
        action: Literal["announced", "deferred"] = (
            "announced" if self.has_text_channel else "deferred"
        )
        # An apology that itself failed is recorded and otherwise dropped. It
        # gets no apology of its own — that is a fixed point that feeds itself,
        # since each failure queues a clip with a new tag and the per-tag dedupe
        # never fires — and it is not deferred either: the QUESTION defers, and
        # handing the gate a line of Jarvis's own prose is how a channel ends up
        # holding a request that was never asked.
        if episode.manufactured(utt):
            return Refused(utterance=utt, reason=reason, action=action)
        # The text still reaches the screen. That is not a consolation prize:
        # the announcement below tells the user to read it there, so mirroring
        # is what makes the refusal recoverable instead of a dead end.
        if self.mirror is not None:
            self.mirror(utt, None)
        if utt.tag not in episode.announced:
            episode.announced.add(utt.tag)
            if self.live is not None:
                apology = Utterance(
                    text=refusal_line(utt.lang, has_text_channel=self.has_text_channel),
                    fidelity="free",
                    lang=utt.lang,
                    tag=f"{utt.tag}:refusal" if utt.tag else "refusal",
                    track=LIVE_TRACK,
                    request_id=utt.request_id,
                )
                episode.apologies.append(apology)
                self._queue.appendleft(apology)
        if action == "deferred" and self.on_defer is not None:
            # A phone leg has no screen and no Telegram binding, so there is
            # nowhere for the options to be read. The question goes back to the
            # gate and waits for a channel that can carry it.
            self.on_defer(utt, reason)
        return Refused(utterance=utt, reason=reason, action=action)
