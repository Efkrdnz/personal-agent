"""Answering a question out loud, by the number you were read.

THE MODEL EMITS AN INDEX AND NEVER A LABEL. That is the whole of this module.
The options were numbered locally from a frozen array
(:func:`jarvis.requests.make_presentation`), read aloud by the deterministic
voice, and the label is looked up from that same array by code. So a model that
mishears "Postgres" as "PostgreSQL", or helpfully improves an option, cannot
change what the user agreed to: there is no path from a string the model
produced to the answer that is stored. The parameter schema below has no place
to put one.

WHICH QUESTION. A desk may have several questions open — one from a build
read-back, one from Claude Code. The tool answers the one THIS DESK MOST RECENTLY
READ ALOUD, tracked by the channel that read it, because "the second one" means
the second option of the thing the user just heard and nothing else. With no
question read here, it refuses and says how many are waiting rather than guessing
at one.

ANSWER CONSTRUCTION IS SHARED, NOT REIMPLEMENTED.
:func:`jarvis.answers.build_answer` is the same function Telegram and the CLI
use. It lived in the Telegram channel until this module needed it; a second
implementation of "turn an index into an Answer" would be a second thing to keep
correct, and the one that drifted would be the one nobody was looking at.
"""

from __future__ import annotations

import sqlite3

from jarvis import answers as ans
from jarvis import requests as rq
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Tool, ToolError

__all__ = ["OPEN_QUESTION", "answer_question", "reread_options", "TOOLS"]

#: The key in :attr:`ToolCtx.extra` naming the request this channel last read
#: aloud. The channel that speaks a question owns putting it there; a channel
#: that cannot read one aloud never sets it, and the tool refuses rather than
#: answering something the user never heard.
OPEN_QUESTION = "open_request_id"


class NoQuestionHere(ToolError):
    """Nothing has been read aloud at this channel, so there is nothing to answer."""

    def __init__(self, waiting: int) -> None:
        if waiting:
            super().__init__(
                f"I haven't read you a question here. There {'is' if waiting == 1 else 'are'} "
                f"{waiting} waiting — ask me to read them."
            )
        else:
            super().__init__("Nothing is waiting on you.")
        self.waiting = waiting


class AlreadySettled(ToolError):
    def __init__(self, where: str) -> None:
        super().__init__(f"That one was already answered {where}.")


def _target(ctx: ToolCtx) -> rq.Request:
    request_id = str(ctx.extra.get(OPEN_QUESTION) or "")
    if not request_id:
        raise NoQuestionHere(len(rq.open_requests(ctx.con)))
    req = rq.get_request(ctx.con, request_id)
    if req is None:
        raise NoQuestionHere(len(rq.open_requests(ctx.con)))
    if req.state != "pending":
        raise AlreadySettled(_where(req.answered_by))
    return req


def _where(answered_by: str | None) -> str:
    """Never interpolate ``answered_by`` raw: it carries a chat id."""
    who = answered_by or ""
    if who.startswith("telegram:"):
        return "on Telegram"
    if who == "timeout":
        return "by the timeout rule"
    return f"on {who}" if who else "elsewhere"


def answer_question(
    ctx: ToolCtx, option: int = 0, options: list[int] | None = None, own_words: str = ""
) -> str:
    """Settle the question this channel last read aloud."""
    req = _target(ctx)
    picks = tuple(int(n) for n in (options or ([option] if option else [])))
    words = own_words.strip() or None

    if picks and words:
        raise ToolError("Tell me either the numbers or your own words, not both.")
    if not picks and not words:
        raise ToolError("Which one? Say the number.")

    try:
        built = ans.build_answer(req, picks=picks, free_text=words)
    except (ans.AnswerShapeError, rq.OptionIndexError, ans.AmbiguousApproval) as exc:
        # Every one of these is a ValueError or a LookupError, so without this the
        # registry's generic handler would say "Sorry — answer_question failed",
        # which tells the user nothing they can act on. These messages are
        # written to be heard.
        raise ToolError(str(exc)) from exc

    won = rq.answer_request(ctx.con, req.id, built, answered_by=ctx.actor, answer_mode="voice")
    if not won:
        fresh = rq.get_request(ctx.con, req.id)
        raise AlreadySettled(_where(fresh.answered_by if fresh else None))
    return _confirmation(req, picks, words)


def _confirmation(req: rq.Request, picks: tuple[int, ...], words: str | None) -> str:
    """Say back what was recorded, from the frozen array — not from what was heard."""
    if words:
        return f"Right — I've put down your own words: {words}"
    labels = rq.labels_for_indices(req.presentation, list(picks))
    return f"Right — {', '.join(labels)}."


def reread_options(ctx: ToolCtx) -> str:
    """Read the current question again, exactly.

    Returns the numbered lines rather than speaking them, because THIS LAYER MAY
    NOT SPEAK: which voice says them is decided by the channel, and at the desk
    that is the deterministic reader (they are exact-tier;
    :class:`jarvis.voice.router.OutputRouter` raises rather than routing them to
    the conversational voice).
    """
    req = _target(ctx)
    pres = req.presentation
    lines = [str(pres["intro"])]
    lines += [f"{item['index']}. {item['label']}" for item in pres["items"]]
    if prompt := str(pres.get("free_text_prompt") or ""):
        lines.append(prompt)
    return "\n".join(lines)


def _open_count(con: sqlite3.Connection) -> int:
    return len(rq.open_requests(con))


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="answer_question",
        description=(
            "Answer the question that was just read to the user, by the NUMBER they said. "
            "Use this the moment they pick one: 'the second one', 'number three', 'SQLite' "
            "when SQLite was option 2 — pass the INDEX, never the text of the option. If they "
            "say something that is not one of the options, pass it as own_words instead. Do "
            "not use this to start work; that is code_build."
        ),
        handler=answer_question,
        parameters={
            "type": "OBJECT",
            "properties": {
                "option": {
                    "type": "INTEGER",
                    "description": "The number the user said, as it was read to them.",
                },
                "options": {
                    "type": "ARRAY",
                    "items": {"type": "INTEGER"},
                    "description": "Several numbers, when the question allows more than one.",
                },
                "own_words": {
                    "type": "STRING",
                    "description": (
                        "What the user said, VERBATIM, when they picked none of the options. "
                        "Their words, not your summary of them, and never an option label."
                    ),
                },
            },
        },
        # Every channel that can read a question aloud can answer one. The phone
        # included: the whole point of the index is that it survives a bad line.
        channels=("desk", "telegram", "phone", "cli"),
    ),
    Tool(
        name="reread_options",
        description=(
            "Read the current question and its numbered options again, word for word. Use "
            "this when the user asks 'what were they again', 'say that again', or sounds "
            "lost. Adds nothing and summarises nothing."
        ),
        handler=reread_options,
        parameters={"type": "OBJECT", "properties": {}},
        channels=("desk", "telegram", "phone", "cli"),
    ),
)
