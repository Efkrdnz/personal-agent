"""The spoken form of an ``AskUserQuestion`` batch: one numbered line per clip.

SPEAKING A QUESTION IS A VOICE CONCERN, and nothing in the Claude Code driver
calls this. The driver needs the payload's SHAPE — which questions, which options,
which answer the CLI will accept — and that lives in :mod:`jarvis.answers`, in the
spine, where every channel can reach it. What a line must be SAID AT is a fidelity
tier, which only exists because there are two voices; that belongs here.

THE NUMBERING IS NOT INVENTED HERE EITHER. :func:`jarvis.answers.slots` owns it
and generates it locally from payload order, so the ordinal a user hears, the
ordinal Telegram prints and the index an answer comes back as are one numbering
with one definition. This module only decides what to say and how exactly it must
be said.

Everything is a pure function over plain data: no connection, no clock, no audio
device. The script a user heard at 9am is the script a test can reproduce at 4pm,
and :mod:`jarvis.voice.chunk` turns it into clips separately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jarvis.answers import FREE_TEXT_PROMPT, questions_of, slots
from jarvis.voice.router import Fidelity

__all__ = [
    "Line",
    "LineKind",
    "script",
]

LineKind = Literal["framing", "question", "option", "free_text"]


@dataclass(frozen=True, slots=True)
class Line:
    """One thing to say, with the fidelity tier it must be said at.

    ``text`` is what a renderer shows; ``label`` is the load-bearing substring on
    an option line, so a verbatim engine can speak the ordinal and the label as
    one clip while a checker can still compare the label alone byte for byte.
    """

    kind: LineKind
    text: str
    fidelity: Fidelity
    index: int | None = None
    label: str | None = None


def script(questions: Mapping[str, Any] | Sequence[Any]) -> tuple[Line, ...]:
    """The ordered, numbered items to be spoken. Generated LOCALLY, in payload order.

    One :class:`Line` per clip, because the latency trick is one utterance per
    option: the whole batch is pre-synthesised while the conversational voice is
    still saying the framing sentence.
    """
    qs = questions_of(questions)
    sl = slots(qs)
    total = len(qs)
    lines: list[Line] = []
    for qi, q in enumerate(qs, start=1):
        multi = bool(q.get("multiSelect"))
        if total > 1:
            lines.append(Line(kind="framing", text=f"Question {qi} of {total}.", fidelity="free"))
        lines.append(Line(kind="question", text=str(q["question"]), fidelity="faithful"))
        if multi:
            lines.append(Line(kind="framing", text="You can pick more than one.", fidelity="free"))
        for s in (s for s in sl if s.question_index == qi):
            # The ordinal immediately precedes the label, with nothing between
            # them: that adjacency is exactly what tools/fidelity_probe.py scores,
            # and a comma or an "option" here would fail its own null baseline.
            lines.append(
                Line(
                    kind="option",
                    text=f"{s.index}. {s.label}",
                    fidelity="exact",
                    index=s.index,
                    label=s.label,
                )
            )
        lines.append(Line(kind="free_text", text=FREE_TEXT_PROMPT, fidelity="free"))
    return tuple(lines)
