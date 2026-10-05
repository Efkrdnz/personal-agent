"""What the user SAID, when the recogniser wrote something that sounds like it.

THE PROBLEM. A general-purpose recogniser hears "coat" or "court" when a
Turkish-accented speaker says "quote": the /kw/ onset reduces to /k/, and the
vowel lands where "coat" lives. Every channel downstream then believes the
wrong word, and the worst one is a build request, because the transcript IS the
contract there (R2): "add a stock coat widget" is filed, tidied and read back
with "coat" in it, character for character, exactly as the fidelity nets demand.

WHY NOT A FIND-AND-REPLACE. Sometimes the user does mean "coat". A blind
substitution turns "where's my winter coat" into nonsense and teaches the user
that the assistant edits what they say, which is worse than mishearing it. So a
correction here is a DECISION made from evidence, and each piece of evidence is
something a reader of this file can point at:

* CUES around the word ("stock", "price", "say", "from the book") argue for the
  meant term; ANTI-CUES ("wear", "winter", "tennis", "judge") argue against.
  Phrases are matched with the candidate substituted in, so "coat of the day"
  is recognised as "quote of the day" and "coat of paint" stays a coat.
* The user's own HISTORY. Each "no, I said quote" is a row in
  ``hearing_fixes``; each "no, I really said coat" is one too. The words those
  sentences share become cues the next time, which is how this gets better at
  one person's speech without anybody editing a list.
* An optional ARBITER — a language model asked "coat or quote?" with the whole
  sentence — consulted only when the evidence is genuinely split, and only
  ever allowed to choose between the two words on the table. It cannot
  rewrite a sentence; it can only vote.

Nothing here touches the raw transcript. :class:`Heard` carries both, because
the raw words are what the user can be shown when a correction was wrong.

THE OTHER HALF lives in the Live layer: :func:`vocabulary` is handed to the
recogniser as ``custom_vocabulary`` so it is less likely to mishear in the
first place, and :func:`instruction` tells the conversational model the same
thing, because it hears the AUDIO, not this transcript.

Spine: standard library only, and every function takes an open connection.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from jarvis.ids import now

__all__ = [
    "SEED",
    "Arbiter",
    "Entry",
    "Fix",
    "Heard",
    "Lexicon",
    "closest",
    "correct",
    "forget",
    "instruction",
    "lexicon",
    "phonetic_key",
    "reject",
    "teach",
    "vocabulary",
]

#: ``(sentence, heard, meant) -> True if the speaker meant ``meant```.
#: Consulted only for a doubtful word; see the module docstring.
Arbiter = Callable[[str, str, str], bool]

#: At or above this, the word is corrected without asking anybody.
CONFIDENT = 2.0
#: Above this and below CONFIDENT, the evidence is split: the arbiter decides if
#: there is one, and otherwise the word is left as heard and reported as doubtful.
DOUBTFUL = 0.5

_NEAR = 4  # tokens either side that count as "right next to it"
_FAR = 12  # beyond this a word is another thought, and says nothing
_MAX_PRIOR = 3.0
_MAX_LEARNED_CUES = 12


@dataclass(frozen=True, slots=True)
class Entry:
    """One word this user says that the recogniser gets wrong."""

    term: str
    heard_as: tuple[str, ...] = ()
    cues: tuple[str, ...] = ()
    anti_cues: tuple[str, ...] = ()
    #: Taught by the user rather than shipped. Shown by `hearing list`; it
    #: earns no score of its own, because the teaching is already a row in
    #: ``hearing_fixes`` and counting it twice would double one sentence.
    taught: bool = False
    #: How the term is written when it is put back, for a name: "Claude", not
    #: "claude". Empty means the term as typed, cased like the word it replaces.
    spelling: str = ""

    def merged(self, other: Entry) -> Entry:
        return Entry(
            term=self.term,
            heard_as=_union(self.heard_as, other.heard_as),
            cues=_union(self.cues, other.cues),
            anti_cues=_union(self.anti_cues, other.anti_cues),
            taught=self.taught or other.taught,
            spelling=self.spelling or other.spelling,
        )


def _w(words: str) -> tuple[str, ...]:
    """A word list written as prose, so a reviewer reads it rather than scrolls it."""
    return tuple(words.split())


def _p(*rows: str) -> tuple[str, ...]:
    """Phrases separated by '|', for the same reason as :func:`_w`."""
    return tuple(x.strip() for row in rows for x in row.split("|") if x.strip())


#: The shipped corrections. Code, not migration rows, because migrations are
#: frozen and these are exactly what improves with use.
SEED: tuple[Entry, ...] = (
    Entry(
        term="quote",
        heard_as=_w("coat court cote kote"),
        cues=(
            # money
            *_w("stock stocks share shares price prices ticker market nasdaq dow crypto"),
            *_w("bitcoin insurance estimate invoice cost costs pay contractor supplier"),
            # words
            *_w("said saying says famous inspirational motivational author book poem"),
            *_w("speech citation cite verbatim exact exactly words line lines einstein"),
            *_w("shakespeare twitter tweet unquote marks mark daily random favourite favorite"),
            # code
            *_w("string single double escape backtick widget api endpoint app feature"),
        ),
        anti_cues=(
            *_w("wear wearing wore winter rain jacket cold warm fur wool leather hang"),
            *_w("hanger button zip sleeve paint primer fresh dog lab"),
            *_w("tennis basketball badminton squash food supreme judge jury lawyer trial"),
            *_w("lawsuit sue sued hearing law legal case royal king queen palace"),
        ),
    ),
    Entry(
        term="claude",
        spelling="Claude",
        heard_as=_w("cloud clod clawed claud clode"),
        cues=_w("code anthropic opus sonnet haiku agent ask tell prompt model plan repo build"),
        anti_cues=(
            *_w("storage aws azure gcp google computing hosting server servers sky rain"),
            *_w("weather dark white nine backup drive icloud dropbox saas repository"),
        ),
    ),
)

#: Phrases that settle it on their own, written WITH the meant term in place.
#: A phrase only counts when substituting the candidate makes it appear.
_PHRASES: dict[str, tuple[str, ...]] = {
    # Each one specific enough that substituting the candidate into an ordinary
    # sentence about a coat does not produce it: "the quote" or "a quote for"
    # would turn "a coat for my son" into evidence.
    "quote": _p(
        "quote of the day | stock quote | price quote | get a quote | get me a quote",
        "a quote from | quote me | quote unquote | end quote | in quotes | quote marks",
        "insurance quote | famous quote | a quote about | a quote by",
    ),
    "claude": _p(
        "claude code | ask claude | tell claude | claude opus | claude sonnet | claude haiku",
        "claude said | claude says",
    ),
}
_ANTI_PHRASES: dict[str, tuple[str, ...]] = {
    "quote": _p(
        "coat of paint | coat of arms | winter coat | rain coat | my coat | your coat",
        "lab coat | fur coat | top coat | base coat | coat rack | tennis court | in court",
        "to court | court case | food court | supreme court | court order | court date",
        "basketball court | high court | court of appeal",
    ),
    "claude": _p(
        "in the cloud | the cloud | cloud storage | cloud computing | google cloud",
        "cloud server | cloud provider | dark cloud | rain cloud | cloud nine | on cloud",
    ),
}

#: Too common to say anything about which word was meant.
_STOP = frozenset(
    _w(
        "a an the and or but if of to in on at for from with by as is are was were be been "
        "it its this that these those i me my you your we our he she they them his her "
        "do does did can could would should will just so not no yes please what which who "
        "how when where there here have has had get got go make about up out some any one"
    )
)


@dataclass(frozen=True, slots=True)
class Fix:
    start: int
    end: int
    heard: str
    meant: str
    score: float
    #: The evidence, in words, so `hearing test` can say WHY.
    why: tuple[str, ...]
    applied: bool
    by: str = "evidence"  # evidence | arbiter


@dataclass(frozen=True, slots=True)
class Heard:
    raw: str
    text: str
    fixes: tuple[Fix, ...] = ()

    @property
    def applied(self) -> tuple[Fix, ...]:
        return tuple(f for f in self.fixes if f.applied)

    @property
    def doubtful(self) -> tuple[Fix, ...]:
        return tuple(f for f in self.fixes if not f.applied and f.score > DOUBTFUL)

    @property
    def changed(self) -> bool:
        return self.text != self.raw


@dataclass(frozen=True)
class Lexicon:
    """A snapshot: the entries, and what the user's history says about each pair."""

    entries: tuple[Entry, ...] = ()
    #: (heard, meant) -> net confirmations, positive when the user has said
    #: "I meant <meant>" more often than "I really said <heard>".
    prior: dict[tuple[str, str], float] = field(default_factory=dict)
    #: meant -> cue words learned from the sentences of confirmed fixes.
    learned: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def entry(self, term: str) -> Entry | None:
        for e in self.entries:
            if e.term == term:
                return e
        return None


# ───────────────────────────── storage ─────────────────────────────


def lexicon(con: sqlite3.Connection, *, extra_terms: Iterable[str] = ()) -> Lexicon:
    """The seeds, the user's taught rows and their history, merged.

    ``extra_terms`` is config's ``voice.vocabulary``: words to bias the
    recogniser toward. They carry no mishearings until the user teaches one, so
    they never rewrite anything on their own — they only reach :func:`vocabulary`
    and :func:`instruction`.
    """
    by_term: dict[str, Entry] = {e.term: e for e in SEED}
    for term, heard_as, cues, anti in con.execute(
        "SELECT term, heard_as, cues, anti_cues FROM lexicon ORDER BY term"
    ):
        row = Entry(
            term=term,
            heard_as=tuple(json.loads(heard_as)),
            cues=tuple(json.loads(cues)),
            anti_cues=tuple(json.loads(anti)),
            taught=True,
        )
        by_term[term] = by_term[term].merged(row) if term in by_term else row
    for raw in extra_terms:
        term = _norm(raw)
        if term and term not in by_term:
            by_term[term] = Entry(term=term)

    prior: Counter[tuple[str, str]] = Counter()
    contexts: dict[str, list[str]] = {}
    for heard, meant, verdict, context in con.execute(
        "SELECT heard, meant, verdict, context FROM hearing_fixes ORDER BY id"
    ):
        # A wrong correction costs more than a right one earns: being edited
        # when you spoke correctly is the failure users do not forgive.
        prior[(heard, meant)] += 1.0 if verdict == "yes" else -1.5
        if verdict == "yes" and context:
            contexts.setdefault(meant, []).append(context)

    learned: dict[str, tuple[str, ...]] = {}
    for meant, sentences in contexts.items():
        # The misheard word is in every one of those sentences by definition;
        # learning it as a cue would make it vote for its own correction.
        entry = by_term.get(meant)
        own = {meant, *(entry.heard_as if entry else ())}
        own |= {h for (h, m) in prior if m == meant}
        seen: Counter[str] = Counter()
        for s in sentences:
            seen.update({w for w in _words(s) if _content(w) and w not in own})
        # A word in two separate confirmed sentences is a pattern; in one, it
        # is an anecdote.
        common = [w for w, n in seen.most_common() if n >= 2]
        learned[meant] = tuple(common[:_MAX_LEARNED_CUES])

    return Lexicon(entries=tuple(by_term.values()), prior=dict(prior), learned=learned)


def teach(
    con: sqlite3.Connection,
    heard: str,
    meant: str,
    *,
    context: str = "",
    actor: str = "user",
    cues: Sequence[str] = (),
) -> Entry:
    """The user said "I said <meant>, not <heard>". Remember it, and learn from it.

    Both writes in one transaction: a confirmation recorded without its mapping
    would bias a pair that cannot fire, and a mapping without its confirmation
    would fire with no history behind it.
    """
    heard_n, meant_n = _norm(heard), _norm(meant)
    if not heard_n or not meant_n:
        raise ValueError("both the word that was heard and the word that was meant are needed")
    if heard_n == meant_n:
        raise ValueError(f"{meant!r} was heard correctly; there is nothing to teach")
    stamp = now()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT heard_as, cues, anti_cues FROM lexicon WHERE term=?", (meant_n,)
        ).fetchone()
        if row is None:
            heard_as: list[str] = []
            known_cues: list[str] = []
            anti: list[str] = []
        else:
            heard_as, known_cues, anti = (json.loads(x) for x in row)
        heard_as = list(_union(heard_as, (heard_n,)))
        known_cues = list(_union(known_cues, (_norm(c) for c in cues if _norm(c))))
        con.execute(
            "INSERT INTO lexicon(term, heard_as, cues, anti_cues, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(term) DO UPDATE SET "
            "heard_as=excluded.heard_as, cues=excluded.cues, updated_at=excluded.updated_at",
            (meant_n, json.dumps(heard_as), json.dumps(known_cues), json.dumps(anti), stamp, stamp),
        )
        con.execute(
            "INSERT INTO hearing_fixes(heard, meant, verdict, context, actor, at) "
            "VALUES (?,?,'yes',?,?,?)",
            (heard_n, meant_n, context.strip(), actor, stamp),
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return Entry(meant_n, tuple(heard_as), tuple(known_cues), tuple(anti), taught=True)


def reject(
    con: sqlite3.Connection, heard: str, meant: str, *, context: str = "", actor: str = "user"
) -> None:
    """The user said "no, I really did say <heard>". A correction went too far.

    Recorded rather than acted on immediately: one rejection outweighs one
    confirmation (see :func:`lexicon`), so the pair needs fresh evidence to fire
    again, but a single "I meant coat" does not forbid "quote" forever.
    """
    heard_n, meant_n = _norm(heard), _norm(meant)
    if not heard_n or not meant_n or heard_n == meant_n:
        raise ValueError("a rejection needs two different words")
    con.execute(
        "INSERT INTO hearing_fixes(heard, meant, verdict, context, actor, at) "
        "VALUES (?,?,'no',?,?,?)",
        (heard_n, meant_n, context.strip(), actor, now()),
    )


def forget(con: sqlite3.Connection, term: str) -> bool:
    """Drop a taught term and its history. Seeds come back; they are code."""
    term_n = _norm(term)
    con.execute("BEGIN IMMEDIATE")
    try:
        gone = con.execute("DELETE FROM lexicon WHERE term=?", (term_n,)).rowcount
        gone += con.execute("DELETE FROM hearing_fixes WHERE meant=?", (term_n,)).rowcount
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return gone > 0


# ───────────────────────────── correcting ─────────────────────────────


def correct(text: str, lex: Lexicon, *, arbiter: Arbiter | None = None) -> Heard:
    """Decide, word by word, whether the recogniser wrote what was said.

    Pure over the snapshot: no connection, so a caller can load the lexicon once
    per turn and correct as many fragments as it likes.
    """
    tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]
    lower = [t[2].lower() for t in tokens]
    forms = _forms(lex)
    fixes: list[Fix] = []
    i = 0
    while i < len(tokens):
        hit = _match(lower, i, forms)
        if hit is None:
            i += 1
            continue
        width, entry, heard_form, meant_form = hit
        score, why = _score(lower, i, width, entry, heard_form, meant_form, lex)
        start, end = tokens[i][0], tokens[i + width - 1][1]
        surface = text[start:end]
        applied, by = score >= CONFIDENT, "evidence"
        if not applied and score > DOUBTFUL and arbiter is not None:
            try:
                applied = bool(arbiter(text, surface, meant_form))
            except Exception:  # noqa: BLE001 - a dead arbiter must leave the words alone
                applied = False
            by = "arbiter"
        if entry.spelling and meant_form.startswith(entry.term):
            meant_form = entry.spelling + meant_form[len(entry.term) :]
        fixes.append(Fix(start, end, surface, _cased(meant_form, surface), score, why, applied, by))
        i += width

    out, cursor = [], 0
    for f in fixes:
        if f.applied:
            out.append(text[cursor : f.start])
            out.append(f.meant)
            cursor = f.end
    out.append(text[cursor:])
    return Heard(raw=text, text="".join(out), fixes=tuple(fixes))


def _forms(lex: Lexicon) -> dict[tuple[str, ...], tuple[Entry, str, str]]:
    """Every surface the recogniser might write, mapped to what it would mean.

    Inflected both ways in step — coats/quotes, coated/quoted, coating/quoting —
    because a lexicon that only knows the bare word misses half of real speech.
    """
    forms: dict[tuple[str, ...], tuple[Entry, str, str]] = {}
    for entry in lex.entries:
        for heard in entry.heard_as:
            for h, m in _inflections(heard, entry.term):
                key = tuple(h.split())
                # First entry wins: two terms claiming one mishearing is a
                # conflict for a human to resolve, not for list order to hide.
                forms.setdefault(key, (entry, h, m))
    return forms


def _match(
    lower: list[str], i: int, forms: dict[tuple[str, ...], tuple[Entry, str, str]]
) -> tuple[int, Entry, str, str] | None:
    for width in (3, 2, 1):
        key = tuple(lower[i : i + width])
        if len(key) == width and key in forms:
            entry, h, m = forms[key]
            return width, entry, h, m
    return None


def _score(
    lower: list[str],
    i: int,
    width: int,
    entry: Entry,
    heard: str,
    meant: str,
    lex: Lexicon,
) -> tuple[float, tuple[str, ...]]:
    why: list[str] = []
    score = 0.0

    base = heard.split()[0] if heard else heard
    bare = _stem_pair(base, entry)
    p = lex.prior.get((bare, entry.term), 0.0)
    if base != bare:
        p += lex.prior.get((base, entry.term), 0.0)
    p = max(-_MAX_PRIOR, min(_MAX_PRIOR, p))
    if p:
        score += p
        why.append(f"history {p:+.1f}")

    # Phrases count only where they cover THIS word: "winter coat" at the end
    # of a sentence says nothing about the "stock coat" at its start.
    meant_tokens = meant.split()
    as_meant = [*lower[:i], *meant_tokens, *lower[i + width :]]
    span_meant = (i, i + len(meant_tokens))
    for phrase in _PHRASES.get(entry.term, ()):
        if _covers(as_meant, phrase, span_meant) and not _covers(lower, phrase, (i, i + width)):
            score += 3.0
            why.append(f"'{phrase}'")
    for phrase in _ANTI_PHRASES.get(entry.term, ()):
        if _covers(lower, phrase, (i, i + width)):
            score -= 3.0
            why.append(f"not: '{phrase}'")

    # Cues weaken with distance and stop counting past _FAR: a build request
    # is a minute of speech, and a "price" forty words earlier is a different
    # thought.
    cues = set(entry.cues) | set(lex.learned.get(entry.term, ()))
    anti = set(entry.anti_cues)
    seen_cues: set[str] = set()
    for j, word in enumerate(lower):
        if i <= j < i + width or word in seen_cues:
            continue
        gap = (i - j) if j < i else (j - (i + width - 1))
        if gap > _FAR:
            continue
        near = gap <= _NEAR
        if word in cues:
            seen_cues.add(word)
            score += 2.0 if near else 1.0
            why.append(word)
        elif word in anti:
            seen_cues.add(word)
            score -= 2.5 if near else 1.25
            why.append(f"not: {word}")
    return score, tuple(why)


def _stem_pair(heard_form: str, entry: Entry) -> str:
    """The bare heard word behind an inflected form, so history is per pair."""
    for h in entry.heard_as:
        if any(form == heard_form for form, _ in _inflections(h, entry.term)):
            return h
    return heard_form


def _covers(tokens: list[str], phrase: str, span: tuple[int, int]) -> bool:
    """True when ``phrase`` occurs in ``tokens`` overlapping ``span``."""
    words = phrase.split()
    n = len(words)
    lo, hi = span
    for start in range(max(0, lo - n + 1), min(len(tokens) - n, hi - 1) + 1):
        if tokens[start : start + n] == words:
            return True
    return False


# ───────────────────────────── the other half ─────────────────────────────


def vocabulary(lex: Lexicon, *, limit: int = 50) -> tuple[str, ...]:
    """Phrases to bias the recogniser toward, most specific first.

    The phrases matter more than the bare words: "stock quote" gives the
    recogniser a context in which "quote" wins, where "quote" alone is one more
    word among a hundred thousand.
    """
    out: list[str] = []
    for e in lex.entries:
        out.extend(_PHRASES.get(e.term, ())[:6])
    for e in lex.entries:
        out.append(e.spelling or e.term)
    return tuple(dict.fromkeys(out))[:limit]


def instruction(lex: Lexicon) -> str:
    """The paragraph that tells the conversational model the same thing.

    It hears the audio, not this module's transcript, so it makes the same
    mistake unless it is told — and it is the one listener that can simply ask.
    """
    lines = []
    for e in lex.entries:
        if not e.heard_as:
            continue
        sounds = " or ".join(f"'{h}'" for h in e.heard_as[:3])
        said = e.spelling or e.term
        lines.append(f"- If you hear {sounds} where '{said}' makes sense, they said '{said}'.")
    if not lines:
        return ""
    return "\n".join(
        [
            "The user speaks English with an accent and some words are misheard:",
            *lines,
            "If a word fits neither reading, ask. When the user says you misheard a word,",
            "call correct_hearing with what you heard and what they meant.",
        ]
    )


# ───────────────────────────── sound ─────────────────────────────


def phonetic_key(word: str) -> str:
    """A coarse, accent-tolerant sound key: quote, coat and court all give 'kot'.

    Not Metaphone. Tuned for the confusions an accent produces rather than for
    English spelling: /kw/ reduces to /k/, w and v merge, th is t, a post-vocalic
    r is dropped (non-rhotic and Turkish-English alike), final consonants
    devoice, and vowels collapse to front ('e') and back ('o').
    """
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    if len(w) > 3 and w.endswith("e") and w[-2] not in "aeiouy":
        w = w[:-1]  # silent e
    for a, b in (
        ("qu", "k"),
        ("ck", "k"),
        ("ph", "f"),
        ("th", "t"),
        ("wh", "v"),
        ("gh", ""),
        ("x", "ks"),
        ("q", "k"),
        ("w", "v"),
        ("z", "s"),
    ):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    w = re.sub(r"(?<=[aeiouy])r(?![aeiouy])", "", w)
    w = re.sub(r"[ouw]+[aeiouy]*", "o", w)
    w = re.sub(r"[aeiy]+", "e", w)
    w = re.sub(r"(.)\1+", r"\1", w)
    devoice = {"d": "t", "b": "p", "g": "k", "v": "f"}
    if w and w[-1] in devoice:
        w = w[:-1] + devoice[w[-1]]
    return w


def closest(meant: str, sentence: str) -> str | None:
    """The word in ``sentence`` that most plausibly was ``meant``, misheard.

    For "no, I said quote" with no mention of what was heard: the sentence it
    refers to is the evidence, and the word that sounds most like "quote" in it
    is the one that was misheard. None when nothing is close — guessing which
    word to retrain on is how a lexicon fills with nonsense.
    """
    target = phonetic_key(meant)
    meant_n = _norm(meant)
    best: tuple[float, str] | None = None
    for word in _words(sentence):
        if word == meant_n or word in _STOP or not target:
            continue
        key = phonetic_key(word)
        d = _distance(key, target) / max(len(key), len(target), 1)
        if d <= 0.34 and (best is None or d < best[0]):
            best = (d, word)
    return best[1] if best else None


# ───────────────────────────── small things ─────────────────────────────

_TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _words(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN.finditer(text)]


def _content(word: str) -> bool:
    return len(word) > 2 and word not in _STOP


def _norm(word: str) -> str:
    return " ".join(re.sub(r"[^a-z' ]", " ", word.lower()).split())


def _union(a: Iterable[str], b: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*a, *b]))


def _inflections(heard: str, meant: str) -> list[tuple[str, str]]:
    """Pairs of (heard form, meant form), inflected on the last word together."""
    pairs = [(heard, meant)]
    if " " in heard or " " in meant:
        h_head, _, h_last = heard.rpartition(" ")
        m_head, _, m_last = meant.rpartition(" ")
        for h, m in _inflections(h_last, m_last)[1:]:
            pairs.append(((h_head + " " + h).strip(), (m_head + " " + m).strip()))
        return pairs
    for suffix in ("s", "ed", "ing", "'s"):
        pairs.append((_inflect(heard, suffix), _inflect(meant, suffix)))
    return pairs


def _inflect(word: str, suffix: str) -> str:
    if suffix == "s" and re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    if suffix in ("ed", "ing") and word.endswith("e"):
        return word[:-1] + suffix
    return word + suffix


def _cased(meant: str, like: str) -> str:
    if like.isupper() and len(like) > 1:
        return meant.upper()
    if meant != meant.lower():
        return meant  # a name keeps its own spelling
    if like[:1].isupper():
        return meant[:1].upper() + meant[1:]
    return meant


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
