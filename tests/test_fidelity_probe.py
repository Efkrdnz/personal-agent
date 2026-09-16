"""The scorer, the null baseline, and the arithmetic that decides the gate.

The load-bearing test in this file is the one that puts WER next to
label_exact_recall on the same transcript and shows them disagreeing by two
orders of magnitude. Everything else is the machinery that makes that
comparison trustworthy: order, ordinal adjacency, insertions, and the ASR
baseline you must subtract before blaming a model for "sequel light".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fidelity_probe import (  # noqa: E402 - the repo root is not on sys.path at import time
    GATE_MIN_RECALL,
    aggregate,
    compare,
    expected_script,
    gate,
    main,
    normalise,
    ordinal_forms,
    rule_of_three_ceiling,
    score_payload,
    wer,
)

LABELS = ["SQLite", "Postgres", "Plain text"]
CLEAN = "1. SQLite 2. Postgres 3. Plain text"


# ───────────────────────────── the metric argument ─────────────────────────────


def test_one_wrong_word_is_invisible_to_wer_and_fatal_to_recall() -> None:
    prose = (
        "Claude Code has a question about storage for the new project, and it would like you "
        "to decide before it writes any migrations, because changing this later means "
        "rewriting the schema layer and every test that touches it. Here are the options it "
        "gave, read exactly as written. "
    )
    truth = prose + CLEAN
    drifted = prose + "1. SQLite 2. MySQL 3. Plain text"

    assert wer(truth, drifted) < 0.02  # noise, to a metric that averages
    score = score_payload(LABELS, drifted, script=truth)
    assert score.label_exact_recall == pytest.approx(2 / 3)  # a wrong build
    assert score.missing == ("Postgres",)


def test_a_clean_reading_scores_one() -> None:
    score = score_payload(LABELS, CLEAN)
    assert score.label_exact_recall == 1.0
    assert score.insertions == 0
    assert score.inversions == 0
    assert wer(CLEAN, CLEAN) == 0.0


# ───────────────────────────── the three conditions ─────────────────────────


def test_a_label_without_its_ordinal_does_not_count() -> None:
    score = score_payload(LABELS, "SQLite, Postgres, or Plain text?")
    assert score.label_exact_recall == 0.0
    assert all(h.present for h in score.hits)  # present, but the binding is gone


def test_the_wrong_ordinal_does_not_count_either() -> None:
    score = score_payload(LABELS, "1. SQLite 3. Postgres 2. Plain text")
    assert [h.ordinal_ok for h in score.hits] == [True, False, False]


def test_a_word_wedged_between_the_ordinal_and_the_label_breaks_the_binding() -> None:
    score = score_payload(LABELS, "1. option SQLite 2. Postgres 3. Plain text")
    assert [h.ordinal_ok for h in score.hits] == [False, True, True]


def test_spoken_ordinals_count_in_both_languages() -> None:
    assert "two" in ordinal_forms(2, "en")
    assert "iki" in ordinal_forms(2, "tr-TR")
    score = score_payload(["SQLite", "Postgres"], "one. sqlite two. postgres")
    assert score.label_exact_recall == 1.0
    tr = score_payload(["SQLite", "Postgres"], "bir. sqlite iki. postgres", lang="tr")
    assert tr.label_exact_recall == 1.0


def test_reordering_is_counted_even_though_every_word_is_present() -> None:
    score = score_payload(LABELS, "2. Postgres 1. SQLite 3. Plain text")
    assert score.inversions == 1
    assert score.label_exact_recall < 1.0
    assert all(h.present for h in score.hits)


def test_an_invented_option_shows_up_as_an_insertion() -> None:
    score = score_payload(LABELS, CLEAN + " 4. MongoDB")
    assert score.label_exact_recall == 1.0  # every real label was said
    assert score.insertions == 2  # "4" and "mongodb" were not in the script
    assert score.insertion_rate > 0


def test_normalisation_is_nfkc_casefold_whitespace_and_nothing_else() -> None:
    assert normalise("  SQLite\n\tSERVER ") == "sqlite server"
    assert normalise("ﬁle") == "file"  # NFKC folds the ligature
    assert "2." in normalise("2. Postgres")  # the ordinal marker survives


def test_expected_script_is_the_ordinal_plus_label_form() -> None:
    assert expected_script(["Yes", "No"]) == "1. Yes 2. No"


# ───────────────────────────── the null baseline ─────────────────────────────


def test_the_asr_null_baseline_is_what_separates_the_model_from_the_microphone() -> None:
    """ "SQLite" comes back as "sequel light" from the READER's own audio too."""
    baseline = aggregate([score_payload(LABELS, "1. sequel light 2. Postgres 3. Plain text")])
    candidate = aggregate([score_payload(LABELS, "1. sequel light 2. Postgres 3. Plain text")])
    diff = compare(candidate, baseline)

    assert baseline.label_exact_recall == pytest.approx(2 / 3)
    assert diff["recall_lost_to_model"] == 0.0  # the transcriber, not the voice
    assert diff["attributable_recall"] == 1.0


def test_a_model_that_drops_a_label_the_baseline_recovered_is_charged_for_it() -> None:
    baseline = aggregate([score_payload(LABELS, CLEAN)])
    candidate = aggregate([score_payload(LABELS, "1. SQLite 2. MySQL 3. Plain text")])
    diff = compare(candidate, baseline)
    assert diff["recall_lost_to_model"] == pytest.approx(1 / 3)
    assert diff["attributable_recall"] == pytest.approx(2 / 3)


def test_aggregation_sums_labels_across_payloads() -> None:
    run = aggregate([score_payload(LABELS, CLEAN) for _ in range(4)])
    assert run.payloads == 4
    assert run.labels == 12
    assert run.label_exact_recall == 1.0
    assert run.as_dict()["label_exact_recall"] == 1.0


# ───────────────────────────── the gate, and the honest arithmetic ───────────


def test_six_hundred_perfect_labels_cannot_demonstrate_the_gate() -> None:
    """200 adversarial payloads is ~600 labels. That is the whole argument."""
    run = aggregate([score_payload(LABELS, CLEAN) for _ in range(200)])
    assert run.labels == 600
    assert run.label_exact_recall == 1.0

    result = gate(run)
    assert not result.passed
    assert result.demonstrable_ceiling == pytest.approx(0.995, abs=1e-3)
    assert any("rule of three" in r for r in result.reasons)
    assert any("1000-label floor" in r for r in result.reasons)


def test_the_gate_can_pass_in_principle_which_is_why_it_is_written_down() -> None:
    run = aggregate([score_payload(LABELS, CLEAN) for _ in range(1200)])
    assert run.labels == 3600
    assert gate(run).passed


def test_one_insertion_or_one_inversion_fails_a_perfect_run() -> None:
    scores = [score_payload(LABELS, CLEAN) for _ in range(1200)]
    scores[0] = score_payload(LABELS, CLEAN + " 4. MongoDB")
    assert not gate(aggregate(scores)).passed

    scores = [score_payload(LABELS, CLEAN) for _ in range(1200)]
    scores[0] = score_payload(LABELS, "2. Postgres 1. SQLite 3. Plain text")
    result = gate(aggregate(scores))
    assert not result.passed
    assert any("inversion" in r for r in result.reasons)


@pytest.mark.parametrize(("n", "ceiling"), [(0, 0.0), (600, 0.995), (3000, 0.999), (30000, 0.9999)])
def test_rule_of_three(n: int, ceiling: float) -> None:
    assert rule_of_three_ceiling(n) == pytest.approx(ceiling, abs=1e-6)


def test_the_gate_threshold_is_the_one_the_architecture_fixed() -> None:
    assert GATE_MIN_RECALL == 0.999


# ───────────────────────────── the CLI ─────────────────────────────


def _write(tmp_path: Path, payloads: list[dict[str, object]]) -> Path:
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"payloads": payloads}), encoding="utf-8")
    return path


def test_the_cli_exits_nonzero_when_the_gate_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, [{"labels": LABELS, "transcript": CLEAN, "baseline": CLEAN}])
    assert main(["--input", str(path)]) == 1
    out = capsys.readouterr().out
    assert "GATE: FAIL" in out
    assert "null baseline" in out


def test_the_cli_speaks_json_and_reports_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(
        tmp_path,
        [{"labels": LABELS, "transcript": "1. SQLite 2. MySQL 3. Plain text", "baseline": CLEAN}],
    )
    main(["--input", str(path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["candidate"]["labels"] == 3
    assert report["vs_baseline"]["recall_lost_to_model"] == pytest.approx(1 / 3)
    assert report["gate"]["passed"] is False


def test_the_demo_runs_and_makes_the_argument(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert "WER" in out
    assert "label_exact_recall" in out
    assert "verbatim path" in out
