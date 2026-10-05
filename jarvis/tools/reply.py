"""A tool result the model should put in its own words, rather than one read out as written.

TWO KINDS OF RESULT, TWO VOICES. At the desk every tool result is read aloud
word for word by the reader voice and the Live model is told to stay silent
(:mod:`jarvis.voice.tools`). That is right for what must be exact — "I'll run
this command; shall I?" — and wrong for data the user asked a question about:
reading out forty lines of ``ipconfig`` is not an answer to "what's my IP".

A :class:`Reply` is still a ``str`` (its short summary), so every caller that
treats results as text keeps working, and a channel that knows no better says
the summary. A channel that does know hands ``detail`` to the model and lets it
answer in Jarvis's own voice.
"""

from __future__ import annotations

__all__ = ["Reply"]


class Reply(str):
    """``str(reply)`` is a short summary; ``.detail`` is data for the model.

    ``aloud`` False (the default) means the reader voice does not speak it: the
    model does, after reading ``detail``. True keeps the reader for a result that
    must be heard exactly and still carries data for the model.
    """

    detail: str
    aloud: bool

    def __new__(cls, said: str, *, detail: str = "", aloud: bool = False) -> Reply:
        reply = super().__new__(cls, said)
        reply.detail = detail
        reply.aloud = aloud
        return reply

    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuild, (str(self), self.detail, self.aloud))


def _rebuild(said: str, detail: str, aloud: bool) -> Reply:
    return Reply(said, detail=detail, aloud=aloud)
