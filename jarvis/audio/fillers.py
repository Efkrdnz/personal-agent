"""Is this transcript only a hesitation sound? ("huh", "hmm", "que", "...")

WHY THIS EXISTS WHEN THE VAD ALREADY FILTERS. A voiced "uh" or "mm" really is
a voice: Silero scores a hum 0.95, and nothing that listens to the sound alone
can tell a 300 ms "mm" from "no". Those turns still reach Gemini, whose
verbatim transcription writes them down as "Hmm." or, for a breath it has
guessed the language of, "que". Kept in the rolling transcript, they end up in
what a build request reads back. So the words are checked too, after the fact.

WHAT IS NOT A FILLER. Anything with a content word in it ("hmm, open notepad"),
single letters ("a" is a word; "o" is Turkish for "that"), and the sounds that
ANSWER a question: "uh-huh", "mm-hmm", "mhm" mean yes and "uh-uh" means no.
Dropping a word costs a requirement; keeping a filler costs a stray "huh". The
rule leans towards keeping.

Pure, standard library only, so any layer can call it.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["is_filler", "without_fillers"]

# Every pattern needs two letters at least, which is what keeps "a", "o", "e"
# and "m" out. Matched against a folded token: casefolded, accents stripped
# ("qué" -> "que", "ähm" -> "ahm"), Turkish dotless ı kept as it is.
_FILLER = re.compile(
    r"""
    h+m+            # hm, hmm, hmmm
    | m{2,}         # mm, mmm
    | h*u+h+        # uh, uhh, huh
    | u+h*m+        # um, umm, uhm
    | u{2,}         # uuu
    | a+h+m* | a{2,}h*      # ah, ahh, ahm (German ähm), aah
    | e+h+m* | e{2,}h*      # eh, ehm, eee (Turkish)
    | e+r+m*        # er, err, erm
    | e+u+h+        # euh (French)
    | o+h+ | o{2,}h*        # oh, ohh, ooh
    | (?:h+a+)+h?   # ha, haha, hah
    | h+e+h+ | (?:he){2,}h? # heh, hehe (never a bare "he")
    | ı{2,}h* | ı+h+        # ıı, ııh (Turkish)
    | q+u+e+        # que: a breath, guessed to be Spanish
    | э+м* | м{2,}  # э, эм, ммм (Russian)
    | [嗯呃额啊哦]+  # en, e, a, o (Chinese)
    """,
    re.VERBOSE,
)

#: Whole utterances that answer: never fillers, whatever they are made of.
_ANSWERS = frozenset(
    {
        "uh-huh", "uh huh", "uhhuh", "mm-hmm", "mm hmm", "mmhmm", "mhm", "mhmm", "mm-hm",
        "uh-uh", "unh-unh", "hı hı", "hıhı", "ıhı", "ıhıh",
    }
)  # fmt: skip

# Bracketed non-speech tags a recogniser may write: [noise], <breath>, (inaudible).
_TAG = re.compile(r"\[[^\]]*\]|<[^>]*>|\([^)]*\)")
_TOKEN = re.compile(r"[^\W\d_]+|\d+")


def is_filler(text: str) -> bool:
    """True when ``text`` holds no word: only hesitation sounds, tags or punctuation.

    Case-insensitive and repetition-proof ("Hmm hmm.", "uh, um"). An empty or
    punctuation-only fragment ("...", "?") is a filler too: nothing was said.
    """
    folded = _fold(_TAG.sub(" ", text))
    if _is_answer(folded):
        return False
    return all(_FILLER.fullmatch(token) for token in _TOKEN.findall(folded))


def _is_answer(folded: str) -> bool:
    return " ".join(re.sub(r"[^\w\s-]", " ", folded).split()) in _ANSWERS


def without_fillers(text: str) -> str:
    """``text`` with its hesitation-only words removed, whitespace folded.

    For JOINED transcript text, never for one streaming fragment: fragments
    can split a word, and " Ah" + "met" filtered piece by piece is "met". Whole
    words are safe. A two-word answer ("uh huh") is kept whole, though each of
    its halves alone would be a filler.
    """
    words = text.split()
    kept: list[str] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and _is_answer(_fold(" ".join(words[i : i + 2]))):
            kept += words[i : i + 2]
            i += 2
            continue
        if not is_filler(words[i]):
            kept.append(words[i])
        i += 1
    return " ".join(kept)


def _fold(text: str) -> str:
    # NFKD splits "é" into "e" + an accent, which is then dropped; "ı" has no
    # decomposition, so Turkish's dotless i survives for its own patterns.
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
