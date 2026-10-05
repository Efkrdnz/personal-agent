""" "No, I said quote." — how a mishearing becomes something Jarvis knows.

Two tools, because there are two different mistakes and they teach opposite
things. ``correct_hearing`` is the recogniser getting a word wrong; it adds a
pair and a confirmation. ``wrong_correction`` is :mod:`jarvis.hearing` getting
a CORRECTION wrong — the user really did say "coat" — and it records a
rejection, which outweighs a confirmation.

THE TRANSCRIPT IS THE EVIDENCE. "No, I said quote" usually does not say what
was heard. The raw transcript the channel hands down (``TRANSCRIPT_RAW``) does,
and :func:`jarvis.hearing.closest` finds the word in it that sounds most like
the one meant. When nothing sounds close, the tool asks rather than guessing,
because a lexicon trained on a guess corrects the wrong word forever.
"""

from __future__ import annotations

from jarvis import hearing
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Tool, ToolError

__all__ = ["HEARD", "TOOLS", "TRANSCRIPT_RAW", "correct_hearing", "wrong_correction"]

#: Keys in :attr:`ToolCtx.extra`. The raw transcript, before any correction, and
#: the :class:`jarvis.hearing.Heard` that turned it into ``transcript``.
TRANSCRIPT_RAW = "transcript_raw"
HEARD = "heard"


def correct_hearing(ctx: ToolCtx, meant: str, heard: str = "") -> str:
    """The user said a word was misheard. Learn the pair."""
    meant = meant.strip()
    if not meant:
        raise ToolError("Which word did you mean?")
    said = str(ctx.extra.get(TRANSCRIPT_RAW) or "")
    heard = heard.strip() or (hearing.closest(meant, said) or "")
    if not heard:
        raise ToolError(
            f"I can't tell which word I got wrong. Say it like: "
            f"when I say {meant}, you hear something else — and tell me what."
        )
    try:
        hearing.teach(ctx.con, heard, meant, context=said, actor=ctx.actor)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return f"Got it. When I hear {heard.lower()}, I'll check whether you meant {meant}."


def wrong_correction(ctx: ToolCtx, said: str, corrected_to: str = "") -> str:
    """The user really did say the word that was 'corrected'. Back off."""
    said_n = said.strip().lower()
    if not said_n:
        raise ToolError("Which word did you really say?")
    meant = corrected_to.strip()
    if not meant:
        heard = ctx.extra.get(HEARD)
        for fix in getattr(heard, "applied", ()):
            if fix.heard.lower() == said_n:
                meant = fix.meant
                break
    if not meant:
        raise ToolError(f"I didn't change {said_n} to anything just now. What did I change it to?")
    try:
        hearing.reject(ctx.con, said_n, meant, context=str(ctx.extra.get(TRANSCRIPT_RAW) or ""))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return f"Sorry — you said {said_n}. I'll be slower to change it to {meant.lower()}."


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="correct_hearing",
        description=(
            "Call when the user says you misheard a word: 'no, I said quote', 'I said "
            "quote, not coat', 'it's quote, Q-U-O-T-E'. meant is the word they meant. "
            "heard is the word you got instead, if they said it or you know it; leave it "
            "empty otherwise. Not for changing an answer to a question — that is "
            "answer_question."
        ),
        handler=correct_hearing,
        parameters={
            "type": "OBJECT",
            "properties": {
                "meant": {"type": "STRING", "description": "The word the user said."},
                "heard": {"type": "STRING", "description": "The word that was heard instead."},
            },
            "required": ["meant"],
        },
    ),
    Tool(
        name="wrong_correction",
        description=(
            "Call when the user says they really did say a word that you changed: 'no, "
            "I actually said coat'. said is the word they really said."
        ),
        handler=wrong_correction,
        parameters={
            "type": "OBJECT",
            "properties": {
                "said": {"type": "STRING", "description": "The word the user really said."},
                "corrected_to": {
                    "type": "STRING",
                    "description": "What it was wrongly changed to, if known.",
                },
            },
            "required": ["said"],
        },
    ),
)
