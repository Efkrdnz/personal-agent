"""The prompt tidier, and the mechanical half of R2's fidelity guarantee.

R2 says the tidied requirement list never adds something the user did not say
and never drops something they did. You cannot make a paraphrase faithful by
asking a second model whether it is faithful, so nothing here does. Every
guarantee in this module is a pure function over strings, and every one of them
can fail the model's output without the model being consulted again.

FOUR NETS, in decreasing strength, honestly labelled.

1. SOURCE-SPAN CONTAINMENT (absolute). Every requirement the model emits must
   carry a ``quote`` that is a *normalised substring of the raw transcript*. An
   invented requirement has no such span, so it is hard-rejected by
   :func:`contains_span` — string containment, no model in the loop. The
   rejected text is kept in ``Spec.rejected`` so the coverage sentence can say
   out loud that something was thrown away.

2. LITERAL-TOKEN ASSERTION (absolute, for the class it covers). Numbers,
   versions, paths, URLs, quoted strings, camelCase/snake_case identifiers,
   acronyms, known tech names and negation phrases are extracted from the raw
   transcript by regex; every one must appear literally in the tidied list.
   This is the highest-damage drift class — "Postgres" becoming "MySQL",
   "no auth" becoming "auth", "port 8080" becoming "port 8000" — and it is
   caught perfectly and mechanically. It over-flags rather than under-flags on
   purpose: a spurious "I may have missed 'no more'" costs two seconds of
   speech, a silently dropped "no auth" costs a rebuild.

3. UNCOVERED SENTENCES (crude, noisy, cheap). A sentence of the transcript that
   no requirement's span touches is read aloud as "nothing in the list covers…".

4. THE APPENDIX (structural). :func:`assemble_prompt` puts the exact confirmed
   bullets *and* the raw transcript into the prompt, so even total prose drift
   in the bullets cannot lose a constraint: Claude still receives the user's own
   words as ground truth.

WHAT IS *NOT* GUARANTEED, stated plainly: containment proves the quote exists,
not that ``text`` follows from the quote. A requirement whose text says the
opposite of its own span would pass net 1. Nets 2 and 4 bound that residual;
nothing here closes it, and pretending otherwise would be the same dishonesty
this module exists to prevent.

EDITS ARE LOCAL LIST MUTATIONS. "drop three", "two should say Postgres not
MySQL", "add: no Docker" go through :func:`drop`, :func:`restate` and
:func:`add`. There is deliberately no re-tidy entry point: a second tidy pass is
a fresh chance to drift, and :func:`tidy` refuses outright if it is handed text
that already contains this module's own section headers.

No model client is imported here and none may be. ``model_call`` is a protocol;
Jarvis passes a Gemini call, the tests pass a fake, and that seam is the reason
the fidelity checks are testable in CI with no credentials.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

__all__ = [
    "APPENDIX_HEADER",
    "CONFIRMED_HEADER",
    "EFFORTS_THAT_CHANGE_BEHAVIOUR",
    "EFFORT_LEVELS",
    "EFFORT_THAT_IS_ALREADY_THE_DEFAULT",
    "PREAMBLE",
    "RESPONSE_SCHEMA",
    "Coverage",
    "Edit",
    "EditIndexError",
    "EffortAsk",
    "LiteralToken",
    "ModeAsk",
    "ModelAsk",
    "ModelCall",
    "Rejected",
    "Requirement",
    "ReTidyRefused",
    "Spec",
    "StaleSpec",
    "TidyFailed",
    "add",
    "apply_edit",
    "assemble_prompt",
    "audit",
    "contains_span",
    "coverage_sentence",
    "drop",
    "effort_note",
    "literal_tokens",
    "missing_literals",
    "mode_note",
    "normalise",
    "parse_edit",
    "parse_effort",
    "parse_mode",
    "parse_model",
    "readback_items",
    "repo_slug",
    "restate",
    "tidy",
    "tidy_prompt",
    "uncovered_sentences",
]


# ───────────────────────────── failures ─────────────────────────────


class TidyFailed(RuntimeError):
    """The model's reply was not a usable Spec. Jarvis says so and re-asks.

    Its own type because the honest spoken response ("I couldn't make sense of
    that, say it again") is different from the response to a *drift* flag.
    """


class ReTidyRefused(RuntimeError):
    """Something already tidied was handed back to :func:`tidy`.

    A re-tidy is a fresh chance to drift, and the drift would be invisible
    because the second pass's spans would all resolve against the first pass's
    prose rather than against the user's words. Edit the list instead.
    """


class EditIndexError(LookupError):
    """An edit named a position that is not in the list.

    Never clamped and never ignored: "drop four" against a three-item list means
    the user is holding a stale read-back in their head, and guessing which item
    they meant is exactly how the wrong requirement gets deleted.
    """


class StaleSpec(RuntimeError):
    """An edit was computed against a revision that is no longer current.

    The race is real: the read-back is spoken at the desk while Telegram shows
    the same numbered list, and "drop three" from one channel can land after the
    other channel already dropped something. Positions shift; ids do not.
    """


# ───────────────────────────── the seam ─────────────────────────────


class ModelCall(Protocol):
    """One JSON-returning model call. Jarvis passes Gemini; tests pass a fake.

    Returning ``str`` (possibly fenced), ``bytes`` or an already-parsed ``dict``
    are all accepted, because every real client disagrees about which it gives
    you and none of that belongs in the fidelity checks.
    """

    def __call__(self, prompt: str) -> str | bytes | dict[str, Any]: ...


# ───────────────────────────── normalisation ─────────────────────────────

#: Left behind by ``str.casefold()`` on U+0130 LATIN CAPITAL LETTER I WITH DOT
#: ABOVE, which folds to "i" + U+0307 rather than to "i".
_COMBINING_DOT_ABOVE = "\u0307"

#: The dotted/dotless I family, folded to one character BEFORE casefold.
#:
#: This is the Turkish trap, and it is worth stating because every naive
#: implementation gets it wrong: ``"İ".casefold()`` is ``"i\u0307"`` (two
#: characters) while ``"I".casefold()`` is "i", and ``"ı".casefold()`` is "ı".
#: So a model that copies "İSTANBUL" out of the transcript and lowercases it to
#: "istanbul" would FAIL containment under a naive casefold, and a legitimate
#: requirement would be hard-rejected as invented.
#:
#: The cost of folding the whole family together is that "sıkı" and "siki"
#: become the same string. That is accepted deliberately: the failure it
#: prevents is a real requirement being thrown away, and the failure it allows
#: requires the model to have invented text that differs from the user's words
#: only in the dots over two i's.
_I_FAMILY = str.maketrans({"İ": "i", "I": "i", "ı": "i", "i": "i"})


def normalise(text: str) -> str:
    """NFKC, Turkish-safe case folding, whitespace collapse.

    The one normalisation used by every containment check in this module. If it
    changes, span containment changes, so it lives in exactly one place.
    """
    out = unicodedata.normalize("NFKC", text)
    out = out.translate(_I_FAMILY)
    out = out.casefold()
    out = out.replace(_COMBINING_DOT_ABOVE, "")
    return " ".join(out.split())


def contains_span(transcript: str, quote: str) -> bool:
    """Is ``quote`` a normalised, contiguous substring of ``transcript``?

    Net 1, in full. An empty or whitespace-only quote is False rather than True:
    the empty string is a substring of everything, which would turn the entire
    guarantee into a no-op and is the single most likely way to break it by
    accident.
    """
    needle = normalise(quote)
    if not needle:
        return False
    return needle in normalise(transcript)


# ───────────────────────────── literal tokens ─────────────────────────────

TokenKind = Literal[
    "quoted",
    "url",
    "path",
    "version",
    "identifier",
    "acronym",
    "number",
    "tech",
    "negation",
]


@dataclass(frozen=True, slots=True)
class LiteralToken:
    """A stretch of the transcript whose exact form is load-bearing.

    ``fold_case`` is per-kind, not global: ``camelCase`` and ``API`` carry
    meaning in their casing, while "no auth" may legitimately be read back as
    "No auth" at the start of a bullet.
    """

    text: str
    kind: TokenKind
    fold_case: bool


#: Matched case-insensitively and required case-insensitively. A curated list is
#: crude, but the alternative — flagging every capitalised word — buries the real
#: flags in noise, and these are the names whose substitution costs a rebuild.
_TECH_TERMS: tuple[str, ...] = (
    "postgresql",
    "postgres",
    "mysql",
    "mariadb",
    "sqlite",
    "redis",
    "mongodb",
    "duckdb",
    "supabase",
    "firebase",
    "docker",
    "kubernetes",
    "nginx",
    "caddy",
    "celery",
    "rabbitmq",
    "kafka",
    "fastapi",
    "flask",
    "django",
    "express",
    "next.js",
    "nuxt",
    "react",
    "vue",
    "svelte",
    "tailwind",
    "sqlalchemy",
    "alembic",
    "pytest",
    "ruff",
    "mypy",
    "poetry",
    "node",
    "deno",
    "bun",
    "python",
    "typescript",
    "javascript",
    "rust",
    "golang",
    "telegram",
    "whatsapp",
    "github",
    "gitlab",
    "stripe",
    "twilio",
    "vercel",
    "railway",
    "cloudflare",
    "oauth",
    "webhook",
    "graphql",
)

#: A negator followed by one of these is a quantifier, not a dropped feature.
_NEGATION_STOPWORDS = frozenset(
    {"more", "less", "longer", "matter", "one", "idea", "way", "problem", "need", "worries"}
)

#: Uppercase runs that are ordinary prose rather than acronyms.
_ACRONYM_STOPWORDS = frozenset({"OK", "AND", "OR", "THE", "BUT", "SO", "I", "A", "AM", "PM"})


def _tech_pattern() -> re.Pattern[str]:
    # Longest first, so "postgresql" is not clipped to "postgres" by alternation.
    body = "|".join(re.escape(t) for t in sorted(_TECH_TERMS, key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{body})(?![\w])", re.IGNORECASE)


_CAMEL_CASE: tuple[TokenKind, re.Pattern[str], int, bool] = (
    "identifier",
    re.compile(r"(?<![\w])[A-Za-z][a-z0-9]+(?:[A-Z][a-zA-Z0-9]*)+(?![\w])"),
    0,
    False,
)
_SNAKE_CASE: tuple[TokenKind, re.Pattern[str], int, bool] = (
    "identifier",
    re.compile(r"(?<![\w])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![\w])"),
    0,
    False,
)

#: (kind, pattern, group, fold_case). Order is PRIORITY: an earlier match wins
#: the characters it covers, so "/usr/lib/python3.11" is one path rather than a
#: path plus a version plus a number.
_TOKEN_PATTERNS: tuple[tuple[TokenKind, re.Pattern[str], int, bool], ...] = (
    # Straight and typographic double quotes only. Single quotes are NOT matched:
    # Turkish attaches suffixes with an apostrophe ("Postgres'i") and English
    # contracts with one ("don't"), so a single-quote rule opens a quoted span at
    # the first apostrophe and swallows the rest of the sentence.
    ("quoted", re.compile(r"[\"“]([^\"”\n]{1,120})[\"”]"), 1, False),
    ("url", re.compile(r"(?:https?://|www\.)[^\s<>\"')]+"), 0, False),
    ("path", re.compile(r"(?<![\w])(?:~|\.{1,2})?/(?:[A-Za-z0-9_.\-]+/?)+"), 0, False),
    ("path", re.compile(r"(?<![\w])[A-Za-z]:\\[^\s]+"), 0, False),
    ("version", re.compile(r"(?<![\w.])v?\d+(?:\.\d+)+(?![\w])"), 0, False),
    # Tech names outrank identifiers so that "Next.js" is matched case-insensitively
    # rather than as a filename whose exact capitalisation nobody agrees on.
    ("tech", _tech_pattern(), 0, True),
    (
        "identifier",
        re.compile(
            r"(?<![\w])[A-Za-z0-9_\-]+\."
            r"(?:py|js|jsx|ts|tsx|json|toml|yaml|yml|md|sql|sh|txt|cfg|ini|env|lock|csv"
            r"|html|css|go|rs)(?![\w])"
        ),
        0,
        False,
    ),
    # Leading capital allowed: "YouTube" and "FastAPI" are exactly as load-bearing
    # as "myVar", and a lowercase-only rule silently drops every product name.
    _CAMEL_CASE,
    _SNAKE_CASE,
    ("acronym", re.compile(r"(?<![\w])[A-Z]{2,6}(?![\w])"), 0, False),
    ("number", re.compile(r"(?<![\w.\-])\d+(?![\w])"), 0, False),
)

#: Extracted in a SECOND pass and allowed to overlap the first, because the
#: polarity is the point: if "Docker" survives into the list but the "no" in
#: front of it does not, the build ships Docker. Both tokens must be present.
_NEGATION_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(?<![\w])(?:no|without|never)\s+([A-Za-z][\w\-]*)", re.IGNORECASE), 1),
    (
        re.compile(
            r"(?<![\w])([A-Za-zÇĞİÖŞÜçğıöşü][\w\-]*)"
            r"\s+(?:yok|olmasın|istemiyorum|kullanma|kullanmayalım)(?![\w])",
            re.IGNORECASE,
        ),
        1,
    ),
)


def literal_tokens(transcript: str) -> list[LiteralToken]:
    """Every token in ``transcript`` whose exact form matters, in spoken order.

    Control phrases ("use Opus 5 with extra-high effort", "in the cloud") are
    masked out first. They are session options, not requirements, and leaving
    them in would flag the "5" of "Opus 5" as a missing literal on every single
    build — a permanent false alarm that trains the user to ignore the real ones.
    """
    masked = _mask(transcript, control_spans(transcript))
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, LiteralToken]] = []

    for kind, pattern, group, fold in _TOKEN_PATTERNS:
        for m in pattern.finditer(masked):
            start, end = m.span(group)
            if any(start < b and a < end for a, b in taken):
                continue
            text = m.group(group)
            if kind == "acronym" and text in _ACRONYM_STOPWORDS:
                continue
            if kind == "path" and len(text.strip("/")) < 2:
                continue
            taken.append(m.span(0))
            found.append((start, LiteralToken(text=text, kind=kind, fold_case=fold)))

    for pattern, group in _NEGATION_PATTERNS:
        for m in pattern.finditer(masked):
            if normalise(m.group(group)) in _NEGATION_STOPWORDS:
                continue
            phrase = " ".join(m.group(0).split())
            found.append((m.start(), LiteralToken(text=phrase, kind="negation", fold_case=True)))

    found.sort(key=lambda pair: pair[0])
    out: list[LiteralToken] = []
    seen: set[tuple[str, str]] = set()
    for _, token in found:
        key = (token.kind, token.text if not token.fold_case else normalise(token.text))
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def missing_literals(transcript: str, texts: Sequence[str]) -> list[LiteralToken]:
    """Which load-bearing tokens of ``transcript`` are absent from ``texts``.

    Net 2. ``texts`` is the tidied requirement list; the check is a literal
    substring test against the joined bullets, so a model that paraphrased
    "port 8080" into "the default port" is caught with no second opinion.
    """
    blob = "\n".join(texts)
    folded = normalise(blob)
    missing: list[LiteralToken] = []
    for token in literal_tokens(transcript):
        present = normalise(token.text) in folded if token.fold_case else token.text in blob
        if not present:
            missing.append(token)
    return missing


# ───────────────────────────── control phrases ─────────────────────────────

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: ``high`` is Claude Code's default on every model that matters here, so
#: "use Opus 5 with high effort" is a NO-OP that will look broken the first time
#: it is demonstrated. Jarvis says so out loud; see :func:`effort_note`.
EFFORT_THAT_IS_ALREADY_THE_DEFAULT = "high"

EFFORTS_THAT_CHANGE_BEHAVIOUR: frozenset[str] = frozenset({"low", "medium", "xhigh", "max"})

_MODEL_RE = re.compile(
    r"(?<![\w])(?P<trigger>use\s+|using\s+|with\s+|on\s+|ile\s+|claude\s+)?"
    r"(?P<family>opus|sonnet|haiku)(?:\s+(?P<version>\d+(?:\.\d+)?))?(?![\w])",
    re.IGNORECASE,
)

_EFFORT_RE = re.compile(
    r"(?<![\w])(?:(?:with|at|on|in)\s+)?"
    r"(low|medium|high|extra[\s\-]?high|x[\s\-]?high|very\s+high|max(?:imum)?"
    r"|düşük|orta|yüksek|çok\s+yüksek|maksimum)"
    r"\s+(?:effort|efor|reasoning|thinking)(?![\w])",
    re.IGNORECASE,
)

#: Tried only when the known levels do not match, so that "with aggressive
#: effort" produces an honest "I don't know that level" instead of silently
#: running at the default and letting the user believe otherwise.
_EFFORT_UNKNOWN_RE = re.compile(
    r"(?<![\w])(?:(?:with|at|on|in)\s+)?([\w\-]+)\s+(?:effort|efor)(?![\w])",
    re.IGNORECASE,
)

#: Words that are grammar rather than a level. Without this, the requirement
#: "log the effort in hours for each task" parses as the effort level "the" —
#: Jarvis then reads out "I don't know that effort level" on a build that never
#: mentioned one, and the phrase is masked out of the literal check as well.
_EFFORT_NON_LEVELS = frozenset(
    {"the", "a", "an", "any", "this", "that", "my", "your", "our", "their", "its", "no", "of"}
)

_CLOUD_RE = re.compile(
    r"(?<![\w])(?:(?:in|on)\s+the\s+cloud|cloud\s+(?:mode|session|da)"
    r"|bulutta|bulut\s+üzerinde|nothing\s+touches\s+(?:my\s+)?local\s+disk)",
    re.IGNORECASE,
)

_LOCAL_RE = re.compile(
    r"(?<![\w])(?:locally|on\s+my\s+machine|on\s+this\s+machine|local\s+mode"
    r"|bu\s+makinede|yerelde|kendi\s+makinemde)",
    re.IGNORECASE,
)

_EFFORT_WORDS: dict[str, str] = {
    "low": "low",
    "düşük": "low",
    "medium": "medium",
    "orta": "medium",
    "high": "high",
    "yüksek": "high",
    "extrahigh": "xhigh",
    "xhigh": "xhigh",
    "veryhigh": "xhigh",
    "çokyüksek": "xhigh",
    "max": "max",
    "maximum": "max",
    "maksimum": "max",
}


@dataclass(frozen=True, slots=True)
class ModelAsk:
    """Which model the user asked for, and the words they used.

    ``alias`` is the CLI's family alias ("opus"), never an invented full model
    id. A spoken "Opus 4.1" is recorded in ``phrase`` and NOT turned into
    ``claude-opus-4-1``: guessing a model id that may not exist starts the wrong
    model, or none, and the failure surfaces minutes later as a dead job.
    """

    alias: str | None
    phrase: str | None


@dataclass(frozen=True, slots=True)
class EffortAsk:
    """Which effort level the user asked for, and whether it does anything."""

    level: str | None
    phrase: str | None

    @property
    def changes_behaviour(self) -> bool:
        return self.level in EFFORTS_THAT_CHANGE_BEHAVIOUR


@dataclass(frozen=True, slots=True)
class ModeAsk:
    """What the user's phrasing asked for on local-vs-cloud.

    ``asked`` may be "cloud" even though :attr:`Spec.mode` is always "local".
    Cloud mode is cut from v1 (ADR 0009) because a deferred permission is
    converted to a hard DENY for cloud sessions, which removes the entire
    away-from-desk path. The ADR's revisit condition is six months of recorded
    phrasing data, so the phrasing is recorded here rather than discarded.
    """

    asked: Literal["unspecified", "local", "cloud", "unclear"]
    phrase: str | None


def _model_matches(transcript: str) -> list[re.Match[str]]:
    """Model mentions, but only when at least one of them was actually a request.

    A bare "opus", "sonnet" or "haiku" is an ordinary English word. "an app that
    writes a haiku every morning" must not silently select the Haiku model, and
    it must not be masked out of the literal check either — masking it would
    stop net 2 from ever flagging the word's disappearance. One explicitly
    triggered mention ("use Opus 5", "on Sonnet") promotes every mention in the
    transcript to a control phrase, so a trailing correction ("no, use Sonnet …
    actually Opus") is still heard.
    """
    matches = list(_MODEL_RE.finditer(transcript))
    if any(m.group("trigger") or m.group("version") for m in matches):
        return matches
    return []


def _effort_unknown_matches(transcript: str) -> list[re.Match[str]]:
    """Unrecognised "<word> effort" phrases, minus the ones that are grammar."""
    return [
        m
        for m in _EFFORT_UNKNOWN_RE.finditer(transcript)
        if normalise(m.group(1)) not in _EFFORT_NON_LEVELS
    ]


def control_spans(transcript: str) -> list[tuple[int, int]]:
    """Character spans of model/effort/local-cloud phrasing, for masking."""
    spans: list[tuple[int, int]] = [m.span() for m in _model_matches(transcript)]
    spans.extend(m.span() for m in _effort_unknown_matches(transcript))
    for pattern in (_EFFORT_RE, _CLOUD_RE, _LOCAL_RE):
        spans.extend(m.span() for m in pattern.finditer(transcript))
    return sorted(spans)


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Blank out ``spans`` with spaces, preserving every other offset."""
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if not chars[i].isspace():
                chars[i] = " "
    return "".join(chars)


def parse_model(transcript: str) -> ModelAsk:
    """ "use Opus 5" -> alias "opus", phrase "Opus 5". Last mention wins."""
    matches = _model_matches(transcript)
    if not matches:
        return ModelAsk(alias=None, phrase=None)
    match = matches[-1]  # a correction ("no, Sonnet") comes after the mistake
    family = match.group("family").lower()
    version = match.group("version")
    phrase = family.capitalize() + (f" {version}" if version else "")
    return ModelAsk(alias=family, phrase=phrase)


def parse_effort(transcript: str) -> EffortAsk:
    """ "with extra-high effort" -> level "xhigh". Last mention wins."""
    matches = list(_EFFORT_RE.finditer(transcript))
    if matches:
        spoken = matches[-1].group(1)  # a correction comes after the mistake
        key = normalise(spoken).replace(" ", "").replace("-", "")
        level = _EFFORT_WORDS.get(key)
        if level is not None:
            return EffortAsk(level=level, phrase=" ".join(spoken.split()))
    unknown = _effort_unknown_matches(transcript)
    if unknown:
        return EffortAsk(level=None, phrase=" ".join(unknown[-1].group(0).split()))
    return EffortAsk(level=None, phrase=None)


def parse_mode(transcript: str) -> ModeAsk:
    """Classify local-vs-cloud phrasing. The answer is still always local.

    Both kinds of phrasing in one transcript is "unclear" rather than a guess:
    the ADR says ask when it is genuinely ambiguous, and silently picking one is
    how "so nothing touches local disk" turns into files on the local disk.
    """
    cloud = _CLOUD_RE.search(transcript)
    local = _LOCAL_RE.search(transcript)
    if cloud and local:
        return ModeAsk(asked="unclear", phrase=" ".join(cloud.group(0).split()))
    if cloud:
        return ModeAsk(asked="cloud", phrase=" ".join(cloud.group(0).split()))
    if local:
        return ModeAsk(asked="local", phrase=" ".join(local.group(0).split()))
    return ModeAsk(asked="unspecified", phrase=None)


# ───────────────────────────── the spec ─────────────────────────────

Origin = Literal["transcript", "user_added", "user_edited"]


@dataclass(frozen=True, slots=True)
class Requirement:
    """One confirmed requirement.

    ``id`` is stable for the life of the spec and is NEVER reused, so the
    activity log can be read back afterwards. ``id`` is not the number the user
    hears: the read-back is numbered by POSITION, which shifts under edits.

    ``quote`` is None only when ``origin`` is not "transcript" — i.e. when the
    user dictated the text themselves during the read-back, in which case their
    words are the source and there is nothing to contain them in.
    """

    id: int
    text: str
    quote: str | None
    origin: Origin = "transcript"


@dataclass(frozen=True, slots=True)
class Rejected:
    """A requirement the model proposed that failed net 1. Never confirmed.

    Kept rather than dropped on the floor, because "I threw one away" is a
    sentence the user must hear before approving the list.
    """

    text: str
    quote: str
    reason: str


@dataclass(frozen=True, slots=True)
class Spec:
    """The tidied build request. Frozen; every edit returns a new one.

    ``mode`` is the literal string "local" and nothing else, because that is all
    v1 can do (ADR 0009). What the user actually asked for lives in ``mode_ask``.
    """

    requirements: tuple[Requirement, ...]
    mode: Literal["local"]
    model: str | None
    effort: str | None
    repo_name: str
    model_ask: ModelAsk
    effort_ask: EffortAsk
    mode_ask: ModeAsk
    rejected: tuple[Rejected, ...] = ()
    revision: int = 0
    next_id: int = 1


# ───────────────────────────── the model prompt ─────────────────────────────

#: Handed to the caller to pass as the model's response schema. A plain dict, so
#: that no SDK type leaks into the spine's dependency-free half of the tree.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repo_name": {"type": "string"},
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["text", "quote"],
            },
        },
    },
    "required": ["requirements"],
}

_TIDY_INSTRUCTIONS = """\
You are turning a spoken, rambling build request into a short numbered list of \
requirements. You are an editor, not a designer.

RULES, and the first one is checked mechanically after you answer:
1. Every requirement MUST carry a "quote" that is copied CHARACTER FOR CHARACTER \
from the transcript below. If you cannot copy a span that supports the \
requirement, do not emit the requirement at all. Requirements whose quote is not \
found in the transcript are deleted without being shown to the user.
2. Never add a requirement the user did not state. No best practices, no \
"you'll also want", no filling in gaps.
3. Never drop a requirement the user did state, however small.
4. Keep every exact form the user used: numbers, ports, versions, file paths, \
URLs, quoted strings, identifiers, product names, and negations such as \
"no auth" or "without Docker". Reproduce them literally in the requirement text.
5. One requirement per line of meaning. Keep the user's own vocabulary.
6. Ignore instructions about which model or effort level to use, and about \
running locally or in the cloud. Those are not requirements.
7. "repo_name" is a short lowercase hyphenated directory name for the project.

Answer with JSON only: {"repo_name": str, "requirements": [{"text": str, \
"quote": str}]}.

TRANSCRIPT:
"""


def tidy_prompt(transcript: str) -> str:
    """The exact prompt handed to ``model_call``. Pure, so it can be asserted on."""
    return _TIDY_INSTRUCTIONS + transcript.strip() + "\n"


# ───────────────────────────── assembly ─────────────────────────────

PREAMBLE = (
    "Build what the user asked for below. The numbered requirements are the exact "
    "wording the user confirmed out loud, one at a time; treat them as the "
    "contract. The appendix is the user's own unedited words, and it is the ground "
    "truth: if the requirements and the appendix disagree, the appendix wins and "
    "you should ask about the difference before building."
)

CONFIRMED_HEADER = "Requirements (as confirmed aloud by the user):"

APPENDIX_HEADER = "Appendix — the user's own words, unedited:"


def assemble_prompt(spec: Spec, transcript: str) -> str:
    """preamble + the exact confirmed bullets + the raw transcript.

    Net 4. The bullets are emitted verbatim and the transcript is emitted
    verbatim, so prose drift in the tidier cannot inject a requirement Claude
    cannot trace, nor hide one the user actually said.
    """
    lines = [PREAMBLE, "", CONFIRMED_HEADER, ""]
    if spec.requirements:
        lines.extend(f"{n}. {req.text}" for n, req in enumerate(spec.requirements, 1))
    else:
        lines.append("(none confirmed — see the appendix)")
    lines.extend(["", APPENDIX_HEADER, "", transcript.strip(), ""])
    return "\n".join(lines)


def readback_items(spec: Spec) -> tuple[tuple[int, str], ...]:
    """(position, exact text) for the verbatim reader. No prose added here.

    The text strings are EXACT-tier: they go to the deterministic reader and
    never through a generative model.
    """
    return tuple((n, req.text) for n, req in enumerate(spec.requirements, 1))


# ───────────────────────────── tidy ─────────────────────────────


def _parse_json(raw: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    # Every JSON-mode model still fences its output some of the time.
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TidyFailed(f"model did not return JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise TidyFailed(f"model returned {type(obj).__name__}, expected an object")
    return obj


def _header_collision(text: str) -> bool:
    """Does this text impersonate one of :func:`assemble_prompt`'s own headers?

    A requirement reading "Appendix — the user's own words, unedited:" would
    split the assembled prompt in a place Claude reads as structure. Cheap to
    refuse, impossible to notice otherwise.
    """
    return CONFIRMED_HEADER in text or APPENDIX_HEADER in text


def tidy(transcript: str, model_call: ModelCall) -> Spec:
    """Tidy a spoken transcript into a Spec, hard-rejecting anything invented.

    Nothing the model says about model, effort or local-vs-cloud is used; those
    are parsed here, deterministically, from the user's own words.
    """
    if not transcript.strip():
        raise TidyFailed("empty transcript")
    if _header_collision(transcript):
        raise ReTidyRefused(
            "this text is already an assembled prompt or a confirmed list; "
            "edit the list with drop/restate/add instead of re-tidying"
        )

    obj = _parse_json(model_call(tidy_prompt(transcript)))
    entries = obj.get("requirements")
    if not isinstance(entries, list):
        raise TidyFailed("model reply has no 'requirements' array")

    kept: list[Requirement] = []
    rejected: list[Rejected] = []
    next_id = 1
    for entry in entries:
        if not isinstance(entry, dict):
            rejected.append(Rejected(text=str(entry), quote="", reason="not an object"))
            continue
        text = " ".join(str(entry.get("text") or "").split())
        quote = str(entry.get("quote") or "").strip()
        if not text:
            rejected.append(Rejected(text=text, quote=quote, reason="empty text"))
        elif _header_collision(text):
            rejected.append(Rejected(text=text, quote=quote, reason="impersonates a prompt header"))
        elif not quote:
            rejected.append(Rejected(text=text, quote=quote, reason="no source span"))
        elif not contains_span(transcript, quote):
            rejected.append(Rejected(text=text, quote=quote, reason="span not in the transcript"))
        else:
            # Ids are OURS. A model-supplied id can collide, repeat or be a
            # string, and every one of those silently breaks "drop three".
            kept.append(Requirement(id=next_id, text=text, quote=quote))
            next_id += 1

    model_ask = parse_model(transcript)
    effort_ask = parse_effort(transcript)
    mode_ask = parse_mode(transcript)
    return Spec(
        requirements=tuple(kept),
        mode="local",
        model=model_ask.alias,
        effort=effort_ask.level,
        repo_name=repo_slug(str(obj.get("repo_name") or ""), transcript),
        model_ask=model_ask,
        effort_ask=effort_ask,
        mode_ask=mode_ask,
        rejected=tuple(rejected),
        revision=0,
        next_id=next_id,
    )


_TR_TRANSLIT = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ç": "c",
        "Ç": "c",
        "ö": "o",
        "Ö": "o",
        "ü": "u",
        "Ü": "u",
    }
)


def repo_slug(proposed: str, transcript: str = "") -> str:
    """A safe directory name, transliterating Turkish rather than stripping it.

    NFKD-stripping "ş" gives "s" but NFKD-stripping "ı" gives "ı" — it has no
    combining form — so a Turkish project name would end up with a raw non-ASCII
    character in a path. The explicit table avoids that.
    """
    for candidate in (proposed, " ".join(transcript.split()[:4])):
        slug = candidate.translate(_TR_TRANSLIT)
        slug = unicodedata.normalize("NFKD", slug)
        slug = "".join(c for c in slug if not unicodedata.combining(c))
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", slug).strip("-").lower()[:40].strip("-")
        if slug:
            return slug
    return "new-project"


# ───────────────────────────── edits ─────────────────────────────


def _check_revision(spec: Spec, expect_revision: int | None) -> None:
    if expect_revision is not None and expect_revision != spec.revision:
        raise StaleSpec(
            f"edit was computed against revision {expect_revision}, "
            f"the list is now at revision {spec.revision}"
        )


def _position(spec: Spec, n: int) -> int:
    if not isinstance(n, int) or isinstance(n, bool):
        raise EditIndexError(f"position must be an int, got {n!r}")
    if not 1 <= n <= len(spec.requirements):
        raise EditIndexError(f"no item {n}: the list has {len(spec.requirements)}")
    return n - 1


def drop(spec: Spec, n: int, *, expect_revision: int | None = None) -> Spec:
    """ "drop three" — remove the item at 1-based POSITION ``n``."""
    _check_revision(spec, expect_revision)
    i = _position(spec, n)
    items = spec.requirements[:i] + spec.requirements[i + 1 :]
    return replace(spec, requirements=items, revision=spec.revision + 1)


def restate(spec: Spec, n: int, text: str, *, expect_revision: int | None = None) -> Spec:
    """ "two should say Postgres not MySQL" — replace the text, keep the id.

    The new text comes from the user's mouth, so it does not have to contain a
    span; ``origin`` records that it is no longer the tidier's wording.
    """
    _check_revision(spec, expect_revision)
    i = _position(spec, n)
    clean = " ".join(text.split())
    if not clean:
        raise ValueError("restate needs text")
    if _header_collision(clean):
        raise ValueError("requirement text may not contain a prompt section header")
    edited = replace(spec.requirements[i], text=clean, origin="user_edited")
    items = spec.requirements[:i] + (edited,) + spec.requirements[i + 1 :]
    return replace(spec, requirements=items, revision=spec.revision + 1)


def add(
    spec: Spec,
    text: str,
    *,
    at: int | None = None,
    expect_revision: int | None = None,
) -> Spec:
    """ "add: no Docker" — append (or insert at 1-based ``at``) a new item."""
    _check_revision(spec, expect_revision)
    clean = " ".join(text.split())
    if not clean:
        raise ValueError("add needs text")
    if _header_collision(clean):
        raise ValueError("requirement text may not contain a prompt section header")
    req = Requirement(id=spec.next_id, text=clean, quote=None, origin="user_added")
    if at is None:
        items = spec.requirements + (req,)
    else:
        i = _position(spec, at)
        items = spec.requirements[:i] + (req,) + spec.requirements[i:]
    return replace(
        spec,
        requirements=items,
        revision=spec.revision + 1,
        next_id=spec.next_id + 1,
    )


@dataclass(frozen=True, slots=True)
class Edit:
    """A parsed spoken edit. ``n`` is a POSITION in the current read-back."""

    kind: Literal["drop", "restate", "add"]
    n: int | None = None
    text: str | None = None


def apply_edit(spec: Spec, edit: Edit, *, expect_revision: int | None = None) -> Spec:
    """Dispatch one parsed edit onto the list."""
    if edit.kind == "drop":
        if edit.n is None:
            raise EditIndexError("a drop edit needs a position")
        return drop(spec, edit.n, expect_revision=expect_revision)
    if edit.kind == "restate":
        if edit.n is None:
            raise EditIndexError("a restate edit needs a position")
        return restate(spec, edit.n, edit.text or "", expect_revision=expect_revision)
    return add(spec, edit.text or "", expect_revision=expect_revision)


_ORDINAL_WORDS: dict[str, int] = {
    "one": 1,
    "first": 1,
    "two": 2,
    "second": 2,
    "three": 3,
    "third": 3,
    "four": 4,
    "fourth": 4,
    "five": 5,
    "fifth": 5,
    "six": 6,
    "sixth": 6,
    "seven": 7,
    "seventh": 7,
    "eight": 8,
    "eighth": 8,
    "nine": 9,
    "ninth": 9,
    "ten": 10,
    "tenth": 10,
}

#: Turkish numerals as STEMS, matched by prefix, because the number carries a
#: case suffix in a spoken command: "üçüncüyü sil" is "delete the third one".
#: "dörd" is listed separately because "dördüncü" softens the final t.
_TR_STEMS: tuple[tuple[str, int], ...] = (
    ("bir", 1),
    ("iki", 2),
    ("üç", 3),
    ("uc", 3),
    ("dörd", 4),
    ("dört", 4),
    ("dord", 4),
    ("beş", 5),
    ("bes", 5),
    ("altı", 6),
    ("alti", 6),
    ("yedi", 7),
    ("sekiz", 8),
    ("dokuz", 9),
    ("on", 10),
)


#: What may follow a Turkish numeral stem: an ordinal suffix, a case suffix, or
#: both. Written in NORMALISED form, because :func:`normalise` folds the whole
#: dotted/dotless I family to "i" ("altıncı" -> "altinci", "yı" -> "yi").
#:
#: A bare prefix test is not enough, and the failure it causes is silent and
#: severe: "bir" is a prefix of "birthday", so "birthday should be optional"
#: would restate item ONE, and "on" is a prefix of "onboarding", so "onboarding
#: should say ..." would restate item TEN. Guessing which item the user meant is
#: precisely what :class:`EditIndexError` exists to prevent, so the suffix must
#: actually look Turkish before the numeral is believed.
_TR_SUFFIX_RE = re.compile(
    r"^(?:inci|uncu|üncü|nci|ncu|ncü)?(?:yi|yu|yü|ni|nu|nü|i|u|ü)?$",
)


def _ordinal(word: str) -> int | None:
    token = normalise(word).strip(".,:;!?")
    if not token:
        return None
    if token.isdigit():
        return int(token) or None
    if token in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[token]
    for stem, value in _TR_STEMS:
        folded = normalise(stem)
        if token.startswith(folded) and _TR_SUFFIX_RE.match(token[len(folded) :]):
            return value
    return None


_EDIT_DROP = re.compile(r"^(?:drop|delete|remove|lose|cut)\s+(?:number\s+)?(\S+)\s*$", re.I)
_EDIT_DROP_TR = re.compile(r"^(\S+)\s*(?:yı|yi|yu|yü|ı|i)?\s*(?:sil|çıkar|kaldır)\s*$", re.I)
_EDIT_ADD = re.compile(r"^(?:add|also|plus)\b\s*[:,]?\s*(.+)$", re.I)
_EDIT_ADD_TR = re.compile(r"^(?:ekle)\s*[:,]?\s*(.+)$", re.I)
_EDIT_RESTATE = re.compile(
    r"^(?:number\s+)?(\S+?)\s+(?:should\s+say|should\s+be|becomes)\s+(.+)$", re.I
)
_EDIT_RESTATE_2 = re.compile(r"^(?:change|make|replace)\s+(?:number\s+)?(\S+?)\s+to\s+(.+)$", re.I)


def parse_edit(said: str) -> Edit | None:
    """Parse one spoken edit locally. Returns None when it is not an edit.

    None means "I did not understand", and the caller must ask again. It must
    never fall back to re-tidying the transcript: an unparsed phrase re-tidied is
    a silent, full-list rewrite triggered by a mumble.
    """
    text = " ".join(said.split())
    if not text:
        return None

    for pattern in (_EDIT_DROP, _EDIT_DROP_TR):
        m = pattern.match(text)
        if m:
            n = _ordinal(m.group(1))
            if n is not None:
                return Edit(kind="drop", n=n)

    for pattern in (_EDIT_RESTATE, _EDIT_RESTATE_2):
        m = pattern.match(text)
        if m:
            n = _ordinal(m.group(1))
            if n is not None and m.group(2).strip():
                return Edit(kind="restate", n=n, text=m.group(2).strip())

    for pattern in (_EDIT_ADD, _EDIT_ADD_TR):
        m = pattern.match(text)
        if m and m.group(1).strip():
            return Edit(kind="add", text=m.group(1).strip())

    return None


# ───────────────────────────── coverage ─────────────────────────────

_SENTENCE_RE = re.compile(r"[^.!?…\n]+")

_FILLER = frozenset(
    {
        "um",
        "uh",
        "er",
        "okay",
        "ok",
        "right",
        "so",
        "well",
        "like",
        "yeah",
        "şey",
        "yani",
        "tamam",
        "peki",
        "evet",
        "hani",
    }
)

_WORD_RE = re.compile(r"[\w'’\-]+")

#: A sentence with fewer content words than this is too slight to be worth
#: reading back as "nothing covers this", and a span with fewer is too slight to
#: be believed as covering one. The same floor on both sides, deliberately.
_MIN_CONTENT_WORDS = 3


def _content_words(text: str) -> list[str]:
    return [
        w
        for w in _WORD_RE.findall(text)
        if normalise(w) not in _FILLER and not normalise(w).isdigit()
    ]


def _substantial(text: str) -> bool:
    return len(_content_words(text)) >= _MIN_CONTENT_WORDS


def uncovered_sentences(transcript: str, spec: Spec) -> list[str]:
    """Sentences of the transcript that no requirement's span touches.

    Net 3, and the weakest one: it is a string-overlap heuristic, it over-flags,
    and it is here because "did I miss this?" spoken aloud costs nothing while a
    missed sentence costs a rebuild.
    """
    # A span only counts as covering a sentence if it has as much substance as
    # the sentences this net bothers to check at all. Without the floor, a model
    # that quotes the single word "Postgres" marks "Also I want Postgres, a
    # nightly backup to S3 and no auth" as covered, and net 3 — already the
    # weakest of the four — goes silent on the sentence it exists to catch.
    quotes = [normalise(r.quote) for r in spec.requirements if r.quote and _substantial(r.quote)]
    masked = _mask(transcript, control_spans(transcript))
    out: list[str] = []
    for m in _SENTENCE_RE.finditer(transcript):
        raw = transcript[m.start() : m.end()].strip()
        if not raw:
            continue
        if not _substantial(masked[m.start() : m.end()]):
            continue
        norm = normalise(raw)
        if any(q in norm or norm in q for q in quotes):
            continue
        out.append(raw)
    return out


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the mechanical checks flagged. Every field is read aloud.

    Every field here MUST be rendered by :func:`coverage_sentence`; there is a
    test that iterates these fields and fails if a new one is added without
    being spoken, because a flag the user never hears is worse than no flag —
    it makes the read-back look complete when it is not.
    """

    invented: tuple[str, ...] = ()
    literals_missing: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.invented or self.literals_missing or self.uncovered)


def audit(transcript: str, spec: Spec) -> Coverage:
    """Run all three flagging nets against the current list."""
    texts = [r.text for r in spec.requirements]
    return Coverage(
        invented=tuple(r.text for r in spec.rejected if r.text),
        literals_missing=tuple(t.text for t in missing_literals(transcript, texts)),
        uncovered=tuple(uncovered_sentences(transcript, spec)),
    )


def _quoted_list(items: Sequence[str]) -> str:
    quoted = [f"'{i}'" for i in items]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def coverage_sentence(cov: Coverage) -> str | None:
    """The "I may have missed…" line, spoken BEFORE the read-back.

    Returns None when nothing was flagged, so the caller says nothing at all
    rather than a reassuring sentence nobody should trust.
    """
    if cov.clean:
        return None
    parts = ["I may have missed something."]
    if cov.literals_missing:
        parts.append(
            f"You said {_quoted_list(cov.literals_missing)}, and I don't see that in the list."
        )
    if cov.invented:
        parts.append(
            f"I threw away {_quoted_list(cov.invented)} "
            "because I couldn't trace it back to your own words."
        )
    if cov.uncovered:
        parts.append(f"And nothing in the list covers {_quoted_list(cov.uncovered)}.")
    return " ".join(parts)


# ───────────────────────────── honest spoken notes ─────────────────────────────


def effort_note(spec: Spec) -> str | None:
    """The honesty line about effort levels, or None when there is nothing to say.

    "use Opus 5 with high effort" is a no-op, and a demo where the user asks for
    more thinking and gets exactly the default is a demo that feels fake. Say so.
    """
    ask = spec.effort_ask
    if ask.level is None:
        if ask.phrase:
            return (
                f"I heard '{ask.phrase}' but I don't know that effort level. "
                "The ones that change anything are low, medium, extra-high and max."
            )
        return None
    who = spec.model_ask.phrase or "Claude Code"
    if ask.level == EFFORT_THAT_IS_ALREADY_THE_DEFAULT:
        return (
            f"{who}, high effort — that's the default, so nothing changes; "
            "say extra-high or max if you want it to think harder."
        )
    return f"{who}, {ask.level} effort."


def mode_note(spec: Spec) -> str | None:
    """The honesty line about local-vs-cloud, or None when nothing was asked."""
    asked = spec.mode_ask.asked
    if asked == "cloud":
        return (
            "You asked for the cloud. This version can only run locally, "
            "so I'm running it here on your machine."
        )
    if asked == "unclear":
        return (
            "I couldn't tell whether you wanted this in the cloud or on this "
            "machine. This version can only run locally, so I'm running it here."
        )
    return None
