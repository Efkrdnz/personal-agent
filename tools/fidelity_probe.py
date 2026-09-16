#!/usr/bin/env python3
"""Score whether a voice actually said the answer keys — and why WER cannot.

WHAT THIS MEASURES. Three numbers over a batch of rendered questions:

``label_exact_recall``
    The fraction of option labels that appear in the transcript as an EXACT
    CONTIGUOUS SUBSTRING after NFKC + casefold + whitespace normalisation, IN
    THE ORIGINAL ORDER, with the CORRECT ORDINAL IMMEDIATELY PRECEDING. All
    three conditions, because all three are load-bearing: the user answers by
    number, so "Postgres" spoken next to the wrong number is not a smaller
    failure than "Postgres" not spoken at all.
``insertion_rate``
    Tokens in the transcript that the script never contained, over the number of
    tokens it did. An invented option is the failure mode nobody instruments,
    and it is the one that gets a user to approve something that was never on
    the list.
``order_inversions``
    Labels that appear, but after a label that should have followed them.
    Reordering is silent: every word is present, the recall looks fine if you
    compute it without the order condition, and "the second one" now means
    something else.

WHY NOT WER. Word error rate averages. A four-question round is roughly sixteen
labels inside maybe two hundred spoken words; one wrong word in one label is
about 0.5% WER — indistinguishable from noise, an excellent-looking number — and
a 100% wrong build, because the user picked option two and option two now says
MySQL. WER answers "does this sound about right?", which is the wrong question
for an answer key. These three numbers answer "is the contract intact?", and
they are deliberately all-or-nothing per label. :func:`wer` is implemented here
ONLY so a report can show the two side by side and make that argument concrete.

THE NULL BASELINE IS NOT OPTIONAL. Run the SAME scorer on the VERBATIM ENGINE'S
OWN AUDIO through the SAME transcription path first. ASR mangles exactly the
strings this project cares about — "SQLite" comes back as "sequel light",
"PostgreSQL" as "post gray sequel" — and without a baseline you will attribute
the transcriber's errors to the model under test, conclude the paraphrase path
is unsafe for a reason that is not true, and (worse) never notice the day the
baseline itself degrades. Every reported number is a DIFFERENCE from the
baseline, and a candidate can only be judged on labels the baseline got right.

THE GATE, FIXED IN ADVANCE so it cannot be relaxed after seeing the data. The
Live (paraphrase) track may carry EXACT-tier text only at::

    label_exact_recall >= 0.999   over >= 1000 labels
    insertions        == 0
    inversions        == 0

AND THE ARITHMETIC IS HONEST ABOUT WHAT THAT COSTS. With zero observed errors in
n trials the 95% upper bound on the true error rate is about 3/n (the rule of
three), so demonstrating 0.999 needs n >= 3000 labels, and the 1000-label line
above is already generous. Two hundred adversarial payloads is about six hundred
labels: a ceiling of roughly 0.995, which cannot demonstrate 0.999 at any useful
confidence no matter how clean the run looks. THAT ASYMMETRY IS THE ARGUMENT FOR
THE VERBATIM PATH. Proving the paraphrase safe costs more than building the
reader: one afternoon of engine wiring beats a week of statistics that will
probably say no. The gate is kept anyway, because a claim that can never be made
should still be written down as a claim.

USAGE. Nothing here touches the network, an audio device or an API key; the
transcription step is injected. Feed it JSON::

    {"payloads": [{"labels": ["SQLite", "Postgres"],
                   "transcript": "one. sqlite two. postgres",
                   "baseline":   "one. sequel light two. postgres"}]}

    python tools/fidelity_probe.py --input run.json --json
    python tools/fidelity_probe.py --demo        # the WER argument, in numbers

Exit status is 0 when the gate passes and 1 when it does not, so it can be a CI
step the day somebody proposes letting the Live voice read labels.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "GATE_MIN_LABELS",
    "GATE_MIN_RECALL",
    "CommandTranscriber",
    "GateResult",
    "LabelHit",
    "PayloadScore",
    "RunScore",
    "Transcriber",
    "aggregate",
    "compare",
    "expected_script",
    "gate",
    "normalise",
    "ordinal_forms",
    "rule_of_three_ceiling",
    "score_payload",
    "tokens",
    "wer",
]

GATE_MIN_RECALL = 0.999
GATE_MIN_LABELS = 1000

#: Spoken ordinals the reader may legitimately produce. The written form ("2.")
#: is what jarvis.voice.script emits; the word forms are what an ASR returns when
#: a voice says it. Turkish is here because the reader speaks Turkish labels and
#: a probe that only knows English would score every Turkish round at zero.
ORDINAL_WORDS: dict[str, dict[int, tuple[str, ...]]] = {
    "en": {
        1: ("one", "first"),
        2: ("two", "second"),
        3: ("three", "third"),
        4: ("four", "fourth"),
        5: ("five", "fifth"),
        6: ("six", "sixth"),
        7: ("seven", "seventh"),
        8: ("eight", "eighth"),
        9: ("nine", "ninth"),
        10: ("ten", "tenth"),
    },
    "tr": {
        1: ("bir", "birinci"),
        2: ("iki", "ikinci"),
        3: ("üç", "üçüncü"),
        4: ("dört", "dördüncü"),
        5: ("beş", "beşinci"),
        6: ("altı", "altıncı"),
        7: ("yedi", "yedinci"),
        8: ("sekiz", "sekizinci"),
        9: ("dokuz", "dokuzuncu"),
        10: ("on", "onuncu"),
    },
}

_PUNCT_EDGE = " .,:;!?()[]{}\"'-–—"


def normalise(text: str) -> str:
    """NFKC + casefold + collapsed whitespace. Nothing else.

    Punctuation is deliberately NOT stripped: the ordinal marker "2." is part of
    the binding being scored, and a normaliser that deletes it would score a
    transcript that lost the numbering as perfect.
    """
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokens(text: str) -> list[str]:
    return [t.strip(_PUNCT_EDGE) for t in normalise(text).split() if t.strip(_PUNCT_EDGE)]


def ordinal_forms(n: int, lang: str = "en") -> tuple[str, ...]:
    """Every way the ordinal for option ``n`` may legitimately appear."""
    words = ORDINAL_WORDS.get(lang.split("-")[0].lower(), ORDINAL_WORDS["en"]).get(n, ())
    return (str(n), *words)


def expected_script(labels: Sequence[str]) -> str:
    """The minimal script the reader would have produced for these labels.

    Used as the denominator for insertions when the caller does not supply the
    real script. It is the ordinal-plus-label form on purpose: that adjacency is
    what jarvis.voice.script emits and what this scorer requires.
    """
    return " ".join(f"{i}. {label}" for i, label in enumerate(labels, start=1))


@dataclass(frozen=True, slots=True)
class LabelHit:
    """What happened to one label."""

    index: int
    label: str
    present: bool
    in_order: bool
    ordinal_ok: bool
    at: int | None = None

    @property
    def exact(self) -> bool:
        return self.present and self.in_order and self.ordinal_ok


@dataclass(frozen=True, slots=True)
class PayloadScore:
    """One rendered question batch, scored."""

    hits: tuple[LabelHit, ...]
    insertions: int
    expected_tokens: int
    inversions: int

    @property
    def labels(self) -> int:
        return len(self.hits)

    @property
    def exact(self) -> int:
        return sum(1 for h in self.hits if h.exact)

    @property
    def label_exact_recall(self) -> float:
        return self.exact / self.labels if self.labels else 0.0

    @property
    def insertion_rate(self) -> float:
        return self.insertions / self.expected_tokens if self.expected_tokens else 0.0

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(h.label for h in self.hits if not h.exact)


@dataclass(frozen=True, slots=True)
class RunScore:
    """A whole run: the numbers the gate reads."""

    labels: int
    exact: int
    insertions: int
    expected_tokens: int
    inversions: int
    payloads: int = 0
    missing: tuple[str, ...] = ()

    @property
    def label_exact_recall(self) -> float:
        return self.exact / self.labels if self.labels else 0.0

    @property
    def insertion_rate(self) -> float:
        return self.insertions / self.expected_tokens if self.expected_tokens else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "payloads": self.payloads,
            "labels": self.labels,
            "label_exact_recall": round(self.label_exact_recall, 6),
            "insertion_rate": round(self.insertion_rate, 6),
            "insertions": self.insertions,
            "order_inversions": self.inversions,
            "missing": list(self.missing),
        }


def _ordinal_precedes(haystack: str, at: int, index: int, lang: str) -> bool:
    """Is the correct ordinal the last thing said before this label?

    "Immediately preceding" means the token before it, allowing only the
    punctuation and whitespace a reader or an ASR would put between "2." and the
    label. Anything else between them — a word, another number — breaks the
    binding, which is the whole point of scoring it separately from presence.
    """
    before = haystack[:at].rstrip(_PUNCT_EDGE)
    if not before:
        return False
    last = before.split()[-1].strip(_PUNCT_EDGE)
    return last in ordinal_forms(index, lang)


def score_payload(
    labels: Sequence[str],
    transcript: str,
    *,
    lang: str = "en",
    script: str | None = None,
) -> PayloadScore:
    """Score one transcript against the labels it was supposed to contain."""
    hay = normalise(transcript)
    cursor = 0
    hits: list[LabelHit] = []
    first_positions: list[int | None] = []
    for i, label in enumerate(labels, start=1):
        needle = normalise(label)
        anywhere = hay.find(needle) if needle else -1
        in_order_at = hay.find(needle, cursor) if needle else -1
        present = anywhere >= 0
        in_order = in_order_at >= 0
        ordinal_ok = in_order and _ordinal_precedes(hay, in_order_at, i, lang)
        if in_order:
            cursor = in_order_at + len(needle)
        hits.append(
            LabelHit(
                index=i,
                label=label,
                present=present,
                in_order=in_order,
                ordinal_ok=ordinal_ok,
                at=anywhere if present else None,
            )
        )
        first_positions.append(anywhere if present else None)

    inversions = 0
    last: int | None = None
    for pos in first_positions:
        if pos is None:
            continue
        if last is not None and pos < last:
            inversions += 1
        last = pos

    expected = tokens(script if script is not None else expected_script(labels))
    extra = Counter(tokens(transcript)) - Counter(expected)
    return PayloadScore(
        hits=tuple(hits),
        insertions=sum(extra.values()),
        expected_tokens=len(expected),
        inversions=inversions,
    )


def aggregate(scores: Sequence[PayloadScore]) -> RunScore:
    missing: list[str] = []
    for s in scores:
        missing.extend(s.missing)
    return RunScore(
        labels=sum(s.labels for s in scores),
        exact=sum(s.exact for s in scores),
        insertions=sum(s.insertions for s in scores),
        expected_tokens=sum(s.expected_tokens for s in scores),
        inversions=sum(s.inversions for s in scores),
        payloads=len(scores),
        missing=tuple(missing),
    )


def compare(candidate: RunScore, baseline: RunScore) -> dict[str, Any]:
    """Candidate minus null baseline — the only numbers that mean anything.

    ``attributable_recall`` is the candidate's recall expressed against what the
    transcription path was able to recover AT ALL. If the baseline recalls 0.94
    because the ASR cannot spell "SQLite", a candidate at 0.94 introduced no
    errors of its own and a candidate at 0.90 introduced four points of them.
    """
    ceiling = baseline.label_exact_recall
    attributable = candidate.label_exact_recall / ceiling if ceiling else 0.0
    return {
        "baseline_recall": round(ceiling, 6),
        "candidate_recall": round(candidate.label_exact_recall, 6),
        "attributable_recall": round(min(attributable, 1.0), 6),
        "recall_lost_to_model": round(max(0.0, ceiling - candidate.label_exact_recall), 6),
        "excess_insertions": candidate.insertions - baseline.insertions,
        "excess_inversions": candidate.inversions - baseline.inversions,
    }


def rule_of_three_ceiling(n: int) -> float:
    """The best recall a CLEAN run of ``n`` labels can demonstrate at 95%.

    Zero errors in n trials bounds the true error rate at about 3/n. So a
    perfect 600-label run demonstrates 0.995 and nothing better, however
    convincing the table looks.
    """
    if n <= 0:
        return 0.0
    return max(0.0, 1.0 - 3.0 / n)


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    demonstrable_ceiling: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "demonstrable_ceiling": round(self.demonstrable_ceiling, 6),
        }


def gate(
    run: RunScore,
    *,
    min_recall: float = GATE_MIN_RECALL,
    min_labels: int = GATE_MIN_LABELS,
) -> GateResult:
    """May the Live track carry EXACT-tier text? Almost certainly not, and here is why."""
    reasons: list[str] = []
    ceiling = rule_of_three_ceiling(run.labels)
    if run.labels < min_labels:
        reasons.append(f"{run.labels} labels is below the {min_labels}-label floor")
    if run.label_exact_recall < min_recall:
        reasons.append(f"label_exact_recall {run.label_exact_recall:.6f} < {min_recall}")
    if run.insertions:
        reasons.append(f"{run.insertions} inserted tokens; the gate requires zero")
    if run.inversions:
        reasons.append(f"{run.inversions} order inversions; the gate requires zero")
    if ceiling < min_recall:
        reasons.append(
            f"a clean run of {run.labels} labels demonstrates at most {ceiling:.4f} at 95% "
            f"(rule of three), so {min_recall} is not demonstrable at this sample size"
        )
    return GateResult(passed=not reasons, reasons=tuple(reasons), demonstrable_ceiling=ceiling)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate. HERE ONLY TO SHOW WHY IT IS THE WRONG METRIC.

    Never gate on this. It is an average, and the failure this project cares
    about is a single word inside a single label — which this number rounds
    away. See the module docstring.
    """
    ref, hyp = tokens(reference), tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


class Transcriber(Protocol):
    """PCM in, text out. Injected, because this file never speaks to a network."""

    def __call__(self, pcm: bytes, rate: int) -> str: ...


@dataclass(frozen=True, slots=True)
class CommandTranscriber:
    """Pipe raw PCM into an external ASR and read its text back on stdout.

    The whole point is that the candidate and the null baseline go through the
    SAME command: an ASR is a measuring instrument, and two different
    instruments cannot be subtracted from one another.
    """

    argv: tuple[str, ...]
    timeout_s: float = 300.0

    def __call__(self, pcm: bytes, rate: int) -> str:
        argv = [a.replace("{rate}", str(rate)) for a in self.argv]
        proc = subprocess.run(  # noqa: S603 - argv is supplied by the operator, no shell
            argv, input=pcm, capture_output=True, check=False, timeout=self.timeout_s
        )
        if proc.returncode != 0:
            raise RuntimeError(f"transcriber exited {proc.returncode}: {proc.stderr[:300]!r}")
        return proc.stdout.decode("utf-8", "replace")


# ───────────────────────────── the CLI ─────────────────────────────


@dataclass
class _Report:
    candidate: RunScore
    baseline: RunScore | None = None
    gate_result: GateResult = field(init=False)

    def __post_init__(self) -> None:
        self.gate_result = gate(self.candidate)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "candidate": self.candidate.as_dict(),
            "gate": self.gate_result.as_dict(),
        }
        if self.baseline is not None:
            out["baseline"] = self.baseline.as_dict()
            out["vs_baseline"] = compare(self.candidate, self.baseline)
        return out

    def as_text(self) -> str:
        lines = [
            f"payloads            {self.candidate.payloads}",
            f"labels              {self.candidate.labels}",
            f"label_exact_recall  {self.candidate.label_exact_recall:.6f}",
            f"insertion_rate      {self.candidate.insertion_rate:.6f} "
            f"({self.candidate.insertions} tokens)",
            f"order_inversions    {self.candidate.inversions}",
        ]
        if self.baseline is not None:
            diff = compare(self.candidate, self.baseline)
            lines += [
                "",
                f"null baseline       {diff['baseline_recall']:.6f}"
                "   (the same scorer on the reader's own audio)",
                f"attributable        {diff['attributable_recall']:.6f}",
                f"excess insertions   {diff['excess_insertions']}",
                f"excess inversions   {diff['excess_inversions']}",
            ]
        lines += ["", f"GATE: {'PASS' if self.gate_result.passed else 'FAIL'}"]
        lines += [f"  - {r}" for r in self.gate_result.reasons]
        if self.candidate.missing:
            shown = ", ".join(self.candidate.missing[:10])
            lines += ["", f"labels not recovered exactly: {shown}"]
        return "\n".join(lines)


def _score_file(path: Path, lang: str) -> _Report:
    data = json.loads(path.read_text(encoding="utf-8"))
    payloads = data["payloads"] if isinstance(data, dict) else data
    cand: list[PayloadScore] = []
    base: list[PayloadScore] = []
    for item in payloads:
        labels = list(item["labels"])
        script = item.get("script")
        cand.append(score_payload(labels, item["transcript"], lang=lang, script=script))
        if item.get("baseline"):
            base.append(score_payload(labels, item["baseline"], lang=lang, script=script))
    return _Report(aggregate(cand), aggregate(base) if base else None)


_DEMO_LABELS = ("SQLite", "Postgres", "Skip tests")

#: Realistic surroundings, because the argument against WER is an argument about
#: RATIOS: the labels are a few hundred bytes inside a couple of hundred spoken
#: words, and that is exactly the denominator that hides the one error that
#: matters. Scored in isolation a wrong label looks catastrophic to WER too;
#: scored in the round it actually ships in, it looks like nothing at all.
_DEMO_FRAMING = (
    "Claude Code has a question about storage for the new project, and it wants you to "
    "pick before it writes any migrations, because changing this later means rewriting "
    "the schema layer and every test that touches it. It says the app is small today, a "
    "few thousand rows at most, but the ingest job it just planned will grow that by "
    "roughly a hundred thousand rows a month once the scraper runs nightly, and it would "
    "rather not guess. It also noticed you already have a local database running for the "
    "other project on this machine, so one of these options costs you nothing to try. "
    "Here are the options it gave, and I will read them to you exactly as they were "
    "written, in order."
)
_DEMO_TAIL = (
    "Or say your own answer if none of those is what you want. Take your time; nothing "
    "is running while it waits, and the job stays parked until you say a number. If you "
    "would rather see them written down, they are on screen and in Telegram as well."
)
_DEMO_OPTIONS = "1. SQLite 2. Postgres 3. Skip tests"
#: One wrong word in one label, and nothing else touched.
_DEMO_DRIFT_OPTIONS = "1. SQLite 2. MySQL 3. Skip tests"
_DEMO_TRUTH = f"{_DEMO_FRAMING} {_DEMO_OPTIONS} {_DEMO_TAIL}"
_DEMO_DRIFT = f"{_DEMO_FRAMING} {_DEMO_DRIFT_OPTIONS} {_DEMO_TAIL}"


def _demo() -> str:
    labels = list(_DEMO_LABELS)
    good = score_payload(labels, _DEMO_TRUTH, script=_DEMO_TRUTH)
    drift = score_payload(labels, _DEMO_DRIFT, script=_DEMO_TRUTH)
    return "\n".join(
        [
            "One label of three carries a different database name, inside one round of "
            f"{len(tokens(_DEMO_TRUTH))} spoken words.",
            "Nothing else changed.",
            "",
            f"  WER                 {wer(_DEMO_TRUTH, _DEMO_DRIFT):.4f}   <- looks like noise",
            f"  label_exact_recall  {drift.label_exact_recall:.4f}   <- the build is wrong",
            f"  insertion_rate      {drift.insertion_rate:.4f}",
            f"  (a clean round      {good.label_exact_recall:.4f})",
            "",
            "WER averages the one failure that matters into two hundred words of prose.",
            "The user picks option two and gets a database nobody chose.",
            "",
            f"A perfect 600-label run demonstrates at most {rule_of_three_ceiling(600):.4f} "
            "at 95% confidence,",
            f"so the {GATE_MIN_RECALL} gate needs >= 3000 labels — which is the argument for "
            "the verbatim path.",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input", type=Path, help="JSON with {payloads: [{labels, transcript, baseline?}]}"
    )
    parser.add_argument("--lang", default="en", help="language of the spoken ordinals")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--demo", action="store_true", help="show why WER is the wrong metric")
    args = parser.parse_args(argv)

    if args.demo or args.input is None:
        print(_demo())
        return 0
    report = _score_file(args.input, args.lang)
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.as_text())
    return 0 if report.gate_result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
