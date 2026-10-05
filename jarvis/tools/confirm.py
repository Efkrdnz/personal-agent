"""Read it back, then act only on the user's own yes.

ONE SHAPE FOR EVERY ACTION THAT NEEDS A YES. Closing an app, shutting down and
running a command all go through two calls of the same tool. The first does
nothing: it remembers the proposal and returns the read-back, which the desk's
reader speaks word for word (it is a plain ``str``, so it is never
paraphrased). The second, with ``confirm`` set, runs only when :func:`granted`
finds that proposal AND the user's own words after it say yes.

THE MODEL CANNOT APPROVE ITSELF. Approval is read from what the USER said — the
desk's input transcript, or the next message typed into a chat — never from
the model's arguments. ``confirm=True`` is the model asking; the transcript is
the answer. A model that calls with ``confirm`` before asking, or after a "no",
or with a different command than the one read back, is refused with a
sentence, and nothing runs.

IN MEMORY, ON PURPOSE, NOT A ``requests`` ROW. A request is a decision that
outlives the conversation: it is routed, escalated to Telegram after ninety
seconds, shown as a card with a button. A yes to "shall I run this?" must do
none of those — approval of a shell command from a phone chat a minute and a
half later is remote code execution with extra steps, and a card with a Yes
button and nobody to act on it is this repository's favourite bug. So a
proposal lives with the conversation that made it, expires in
:data:`EXPIRES_S`, and the durable record is the hash-chained event log
(``confirm.proposed`` / ``confirm.granted`` / ``confirm.refused``) plus the
effect row the tool records with ``confirmed_by``.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jarvis.bus import publish
from jarvis.ids import nid
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import ToolError

__all__ = [
    "CONFIRMATIONS",
    "DIRECT_HUMAN",
    "EXPIRES_S",
    "HEARD_SINCE",
    "MARK",
    "Confirmations",
    "Proposal",
    "TypedTurns",
    "answer_in",
    "ask",
    "granted",
]

#: ``ctx.extra`` keys. The conversation's owner (the desk's LiveTools, a chat)
#: puts them there; a tool never builds them.
CONFIRMATIONS = "confirmations"
MARK = "mark"
HEARD_SINCE = "heard_since"
#: True only when a person invoked the tool directly (the window's Tools tab):
#: the click is the yes.
DIRECT_HUMAN = "direct_human"

#: Long enough to hear a read-back and answer; short enough that an old yes
#: cannot be found by a model wandering back to it later.
EXPIRES_S = 90.0

#: How long the second call waits for the user's words to be transcribed. The
#: tool call can reach us a moment before the transcript of the "yes" that
#: caused it.
WAIT_S = 2.0

_NEGATIVE = frozenset(
    {
        "no", "nope", "nah", "dont", "stop", "cancel", "wait", "never", "not", "hold",
        "abort", "negative",
        # Turkish, folded (see _fold): hayır, yok, iptal, dur, vazgeç, istemiyorum,
        # etme, yapma, bekle.
        "hayir", "yok", "iptal", "dur", "vazgec", "istemiyorum", "etme", "yapma", "bekle",
    }
)  # fmt: skip
_AFFIRMATIVE = frozenset(
    {
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "okey", "confirm", "confirmed",
        "proceed", "affirmative", "correct", "absolutely", "definitely", "indeed",
        # evet, tamam, olur, aynen, onaylıyorum, onay, peki, tabii, yap.
        "evet", "tamam", "olur", "aynen", "onayliyorum", "onay", "peki", "tabii", "tabi", "yap",
    }
)  # fmt: skip
_AFFIRMATIVE_PHRASES = (
    ("do", "it"),
    ("go", "ahead"),
    ("go", "on"),
    ("please", "do"),
    ("run", "it"),
    ("of", "course"),
    ("sounds", "good"),
)
_FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "ğ": "g", "ç": "c", "ö": "o", "ü": "u"})


def answer_in(words: str) -> bool | None:
    """True for a yes, False for a no, None when the words say neither.

    A no anywhere wins: "yes — no, wait" is a no. A false no costs a repeated
    question; a false yes runs a command.
    """
    tokens = _fold(words)
    if not tokens:
        return None
    if any(t in _NEGATIVE for t in tokens):
        return False
    if any(t in _AFFIRMATIVE for t in tokens):
        return True
    pairs = set(zip(tokens, tokens[1:], strict=False))
    if any(p in pairs for p in _AFFIRMATIVE_PHRASES):
        return True
    return None


def _fold(words: str) -> list[str]:
    text = unicodedata.normalize("NFKC", words).translate(_FOLD).casefold()
    text = text.replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", text)


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    tool: str
    key: str
    mark: Any
    readback: str
    effect: str
    created: float


@dataclass
class Confirmations:
    """One conversation's pending proposals. Thread-safe: tools run in worker threads."""

    expires_s: float = EXPIRES_S
    wait_s: float = WAIT_S
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _pending: dict[tuple[str, str], Proposal] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def propose(self, *, tool: str, key: str, mark: Any, readback: str, effect: str) -> Proposal:
        """Remember one proposal. A newer one for the same action replaces the older."""
        proposal = Proposal(
            id=nid("cf"),
            tool=tool,
            key=key,
            mark=mark,
            readback=readback,
            effect=effect,
            created=self.clock(),
        )
        with self._lock:
            self._expire()
            self._pending[(tool, key)] = proposal
        return proposal

    def pending(self, tool: str, key: str) -> Proposal | None:
        with self._lock:
            self._expire()
            return self._pending.get((tool, key))

    def settle(self, proposal: Proposal) -> bool:
        """Forget it. True only for the caller that removed it: one yes, one action."""
        with self._lock:
            if self._pending.get((proposal.tool, proposal.key)) is proposal:
                del self._pending[(proposal.tool, proposal.key)]
                return True
            return False

    def _expire(self) -> None:
        cutoff = self.clock() - self.expires_s
        for k in [k for k, p in self._pending.items() if p.created < cutoff]:
            del self._pending[k]


@dataclass
class TypedTurns:
    """What the user typed into a chat, in order: the chat's :data:`MARK` and :data:`HEARD_SINCE`.

    In a typed conversation the answer to a read-back is simply the next
    message: a mark is a count of messages, and the words "since" it are every
    message typed after the one that caused the proposal.
    """

    _said: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def said(self, text: str) -> None:
        """Record one message, before the model sees it."""
        with self._lock:
            self._said.append(text)

    def mark(self) -> int:
        with self._lock:
            return len(self._said)

    def since(self, mark: Any) -> str:
        if not isinstance(mark, int):
            return ""
        with self._lock:
            return " ".join(self._said[mark:])

    def keys(self, box: Confirmations) -> dict[str, Any]:
        """The ``ctx.extra`` entries a tool needing a yes reads, for this conversation."""
        return {CONFIRMATIONS: box, MARK: self.mark, HEARD_SINCE: self.since}


def ask(ctx: ToolCtx, *, tool: str, key: str, readback: str, effect: str) -> str:
    """The first call: remember the proposal, log it, return the read-back. Nothing happens."""
    box = _box(ctx)
    mark_fn = ctx.extra.get(MARK)
    mark = mark_fn() if callable(mark_fn) else None
    proposal = box.propose(tool=tool, key=key, mark=mark, readback=readback, effect=effect)
    _log(ctx, "confirm.proposed", proposal, {"readback": readback})
    return f"{readback.rstrip()} Shall I go ahead? Say yes, or no."


def granted(ctx: ToolCtx, *, tool: str, key: str) -> Proposal:
    """The second call. Returns the proposal only on the user's own yes; raises otherwise."""
    box = _box(ctx)
    proposal = box.pending(tool, key)
    if ctx.extra.get(DIRECT_HUMAN) is True:
        # The person pressed the button themselves; that press is the yes.
        if proposal is not None:
            box.settle(proposal)
        else:
            proposal = Proposal(nid("cf"), tool, key, None, "", "", box.clock())
        _log(ctx, "confirm.granted", proposal, {"by": "click"})
        return proposal
    if proposal is None:
        _log_refusal(ctx, tool, "not asked")
        raise ToolError(
            "I haven't read that back to you yet, so I haven't done it. Ask again and "
            "I'll tell you exactly what I'd do first."
        )
    verdict = _wait_for_answer(ctx, box, proposal)
    if verdict is True:
        if not box.settle(proposal):
            raise ToolError("That was already done once.")
        _log(ctx, "confirm.granted", proposal, {"by": "said"})
        return proposal
    if verdict is False:
        box.settle(proposal)
        _log(ctx, "confirm.refused", proposal, {"why": "said no"})
        raise ToolError("You said no, so I haven't.")
    _log(ctx, "confirm.refused", proposal, {"why": "no yes heard"})
    raise ToolError("I didn't hear a yes, so I haven't done it. Say yes if you want me to.")


def _wait_for_answer(ctx: ToolCtx, box: Confirmations, proposal: Proposal) -> bool | None:
    heard = ctx.extra.get(HEARD_SINCE)
    if not callable(heard):
        return None  # no way to hear the user: fail closed
    deadline = box.clock() + box.wait_s
    while True:
        verdict = answer_in(str(heard(proposal.mark) or ""))
        if verdict is not None or box.clock() >= deadline:
            return verdict
        box.sleep(0.1)


def _box(ctx: ToolCtx) -> Confirmations:
    box = ctx.extra.get(CONFIRMATIONS)
    if not isinstance(box, Confirmations):
        # No conversation to hold a yes (a scheduled job, a bare dispatch):
        # nothing that needs one can run here.
        raise ToolError("I can't ask you to confirm from here, so I won't do that.")
    return box


def _log(ctx: ToolCtx, kind: str, proposal: Proposal, extra: Mapping[str, Any]) -> None:
    payload = {
        "tool": proposal.tool,
        "proposal": proposal.id,
        "effect": proposal.effect,
        "channel": ctx.channel,
        **extra,
    }
    publish(ctx.con, kind, ctx.actor, payload, redactor=ctx.extra.get("redactor"))


def _log_refusal(ctx: ToolCtx, tool: str, why: str) -> None:
    publish(
        ctx.con,
        "confirm.refused",
        ctx.actor,
        {"tool": tool, "channel": ctx.channel, "why": why},
        redactor=ctx.extra.get("redactor"),
    )
