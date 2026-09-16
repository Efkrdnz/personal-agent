"""Cutting text into clips: one per option, and one per sentence for prose.

TWO MECHANICS REMOVE THE DEAD AIR, and both live here. Neither local synthesis
nor Gemini TTS streams, so a naively chunked question is four seconds of silence
at the exact moment the user is waiting to decide something.

*ONE UTTERANCE PER OPTION.* A question renders to N+2 clips of one to two
seconds — framing, the question, every option, the free-text tail — instead of
one long recording. The first clip can start playing while the last is still
being made, and "repeat option two" is a seek rather than a re-synthesis.

*PRE-SYNTHESIS FIRES THE INSTANT THE QUESTION ARRIVES.* :func:`presynthesise`
gathers over every clip while the conversational voice is still saying "Claude
Code has a question about storage", so all TTS latency hides behind Live's own
utterance. The cache makes the second round free.

THE NUMBERING IS NOT INVENTED HERE. :mod:`jarvis.cc.narrate` owns it, generates
it locally from the payload order, and hands back lines already tagged with a
fidelity tier. This module translates those lines into utterances and adds the
episode's earcon brackets. Two modules, one numbering: there is exactly one
place in the tree where an option gets a number, which is what lets ``jarvis
log`` show what the user actually heard.

The whole read-options episode is PINNED to the reader, including the framing
and the tail, which are ``free`` text. Switching voices mid-episode is what
makes the seam jarring rather than legible; the tiers stay accurate because they
describe the TEXT's requirement, not who happens to be reading it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from jarvis.cc import narrate
from jarvis.voice.router import VERBATIM_TRACK, EarconMark, Fidelity, Utterance
from jarvis.voice.verbatim import VerbatimSpeaker

__all__ = [
    "ABBREVIATIONS",
    "MAX_RUN_CHARS",
    "SentenceSplitter",
    "disclosure_clip",
    "narration_clips",
    "option_clips",
    "presynthesise",
    "readback_clips",
    "sentences",
    "texts_to_prefetch",
]


def _bracket(i: int, total: int) -> EarconMark:
    """Where the 150 ms tone goes on clip ``i`` of an episode of ``total``.

    The earcon brackets the EPISODE. A one-clip episode gets both tones; a beep
    between every option would teach the user nothing and sound like an alarm.
    """
    if total <= 1:
        return "both"
    if i == 0:
        return "open"
    return "close" if i == total - 1 else "none"


def option_clips(
    questions: Mapping[str, Any] | Sequence[Any],
    *,
    lang: str = "en",
    tag: str = "options",
    request_id: str | None = None,
) -> tuple[Utterance, ...]:
    """The spoken form of one ``AskUserQuestion`` batch, one clip per line.

    Fidelity comes straight from :func:`jarvis.cc.narrate.script`: the option
    lines are ``exact`` and carry both the ordinal and the label, because the
    ordinal-to-label binding is itself load-bearing — the user answers with a
    number and the number must have been spoken next to the right words.
    """
    lines = narrate.script(questions)
    clips: list[Utterance] = []
    for i, line in enumerate(lines):
        earcon = _bracket(i, len(lines))
        clips.append(
            Utterance(
                text=line.text,
                fidelity=line.fidelity,
                lang=lang,
                tag=tag,
                index=line.index,
                label=line.label,
                track=VERBATIM_TRACK,
                earcon=earcon,
                request_id=request_id,
            )
        )
    return tuple(clips)


def readback_clips(
    items: Sequence[str],
    *,
    lang: str = "en",
    tag: str = "readback",
    request_id: str | None = None,
) -> tuple[Utterance, ...]:
    """The confirmed requirement list, numbered, one clip each, EXACT.

    The user edits this list by number — "drop three", "two should say Postgres
    not MySQL" — so the numbering must be as stable and as audible as it is for
    options, and the strings must be the ones that will actually be sent.
    """
    clips: list[Utterance] = []
    kept = [item for item in items if item.strip()]
    for n, item in enumerate(kept, start=1):
        clips.append(
            Utterance(
                text=f"{n}. {item}",
                fidelity="exact",
                lang=lang,
                tag=tag,
                index=n,
                label=item,
                track=VERBATIM_TRACK,
                earcon=_bracket(n - 1, len(kept)),
                request_id=request_id,
            )
        )
    return tuple(clips)


def disclosure_clip(
    text: str,
    *,
    lang: str = "en",
    tag: str = "disclosure",
    request_id: str | None = None,
) -> Utterance:
    """The AI-disclosure line. EXACT, because it is contract text somebody may quote."""
    return Utterance(
        text=text,
        fidelity="exact",
        lang=lang,
        tag=tag,
        track=VERBATIM_TRACK,
        earcon="both",
        request_id=request_id,
    )


def narration_clips(
    text: str,
    *,
    lang: str = "en",
    tag: str = "narration",
    fidelity: Fidelity = "free",
    request_id: str | None = None,
) -> tuple[Utterance, ...]:
    """Prose split into sentences. FREE by default — this is progress narration."""
    return tuple(
        Utterance(
            text=sentence,
            fidelity=fidelity,
            lang=lang,
            tag=tag,
            request_id=request_id,
        )
        for sentence in sentences(text)
    )


def texts_to_prefetch(clips: Iterable[Utterance]) -> tuple[tuple[str, str], ...]:
    """Unique ``(text, lang)`` for every clip the reader will have to synthesise.

    Free-tier clips that are going to the conversational voice are skipped:
    synthesising them would be work for audio nobody plays.
    """
    seen: dict[tuple[str, str], None] = {}
    for clip in clips:
        if clip.track == VERBATIM_TRACK or clip.load_bearing:
            seen.setdefault((clip.text, clip.lang), None)
    return tuple(seen)


async def presynthesise(speaker: VerbatimSpeaker, clips: Iterable[Utterance]) -> int:
    """Warm the cache for a whole episode. Returns how many clips are ready.

    Never raises: a prefetch that fails is a slow question, while a prefetch
    that raises is a lost one. The refusal decision belongs at speaking time.
    """
    by_lang: dict[str, list[str]] = {}
    for text, lang in texts_to_prefetch(clips):
        by_lang.setdefault(lang, []).append(text)
    ready = 0
    for lang, texts in by_lang.items():
        ready += await speaker.prefetch(texts, lang)
    return ready


# ───────────────────────────── sentence splitting ─────────────────────────────

#: Trailing dots that do not end a sentence. Lower-cased for comparison; the
#: list is short on purpose, because every entry is a guess about prose nobody
#: has written yet and a missed split costs a slightly long clip, not fidelity.
ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "e.g.",
        "i.e.",
        "etc.",
        "vs.",
        "mr.",
        "mrs.",
        "ms.",
        "dr.",
        "prof.",
        "st.",
        "no.",
        "fig.",
        "inc.",
        "ltd.",
        "sr.",
        "jr.",
        "vb.",  # "ve benzeri" — Turkish "etc."
        "örn.",  # "örneğin" — Turkish "e.g."
    }
)

#: A streamed model can emit hundreds of characters with no terminator at all
#: (a code block, a list, a language whose punctuation the splitter does not
#: know). Speaking nothing until the stream ends is worse than speaking a clause,
#: so a run this long is cut at its last space.
MAX_RUN_CHARS = 400

_TERMINATORS = ".!?…"
_CLOSERS = ')]}"\'’”»'
_WORD = re.compile(r"[^\s]+$")


class SentenceSplitter:
    """Incremental splitter for streamed output. Instantiate one per stream.

    Streaming is the whole difficulty. A terminator at the end of the buffer may
    be the end of a sentence or the middle of "Dr" — the next chunk decides — so
    a boundary is only committed once whitespace has actually arrived after it.
    ``flush()`` is what ends the stream, and it is the only thing that will
    speak a final clause with no punctuation.
    """

    def __init__(self, max_run_chars: int = MAX_RUN_CHARS) -> None:
        self._buf = ""
        self._max_run_chars = max_run_chars

    def feed(self, chunk: str) -> list[str]:
        """Add text; return every sentence that is now definitely complete."""
        self._buf += chunk
        out: list[str] = []
        while True:
            cut = self._boundary(self._buf)
            if cut is None:
                break
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> list[str]:
        """Everything left, split as far as possible. Ends the stream."""
        rest = self._buf.strip()
        self._buf = ""
        if not rest:
            return []
        out: list[str] = []
        while True:
            cut = self._boundary(rest + " ")
            if cut is None or cut >= len(rest):
                break
            out.append(rest[:cut].strip())
            rest = rest[cut:].lstrip()
        if rest.strip():
            out.append(rest.strip())
        return [s for s in out if s]

    def _boundary(self, buf: str) -> int | None:
        for i, ch in enumerate(buf):
            if ch in _TERMINATORS:
                end = i + 1
                while end < len(buf) and buf[end] in _CLOSERS:
                    end += 1
                if end >= len(buf):
                    # The terminator is the last thing we have. It may yet turn
                    # out to be "Dr" or "3." — wait for the next chunk.
                    break
                if not buf[end].isspace():
                    # "router.py", "3.14", "v2.1.273": a dot with a word glued to
                    # its right is inside a token, not between two sentences.
                    continue
                if self._suppressed(buf[: i + 1]):
                    continue
                return end
        if len(buf) > self._max_run_chars:
            window = buf[: self._max_run_chars]
            space = window.rfind(" ")
            if space > 0:
                return space
        return None

    @staticmethod
    def _suppressed(upto: str) -> bool:
        if not upto.endswith("."):
            return False
        match = _WORD.search(upto)
        if match is None:
            return False
        token = match.group(0).lower()
        if token in ABBREVIATIONS:
            return True
        # "1." is a list marker or an ordinal — and in an option clip it is the
        # binding between a number and a label, which must never be cut in two.
        return token[:-1].isdigit()


def sentences(text: str, *, max_run_chars: int = MAX_RUN_CHARS) -> list[str]:
    """Split a complete string. The one-shot form of :class:`SentenceSplitter`."""
    splitter = SentenceSplitter(max_run_chars)
    out = splitter.feed(text)
    out.extend(splitter.flush())
    return out
