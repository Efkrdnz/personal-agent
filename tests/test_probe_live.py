"""The S4 probe, exercised on a machine that can never run it for real.

A probe nobody has executed is a probe that fails at minute one of the hour it
was supposed to measure. There is no API key here and there never will be one in
CI, so ``--dry-run`` puts the scripted transport behind the same code path and
this file runs it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import probe_live  # noqa: E402
from tools.probe_live import main  # noqa: E402 - the repo root is not on sys.path at import time


def test_the_dry_run_answers_all_four_questions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run", "--concurrency", "2"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["model"] == "gemini-3.8-live"
    assert report["concurrency"]["opened_concurrently"] == 2
    answers = report["answers"]
    assert answers["model_id_exists"] is True
    assert answers["go_away_seen"] is True
    assert answers["resumed_after_go_away"] is True
    assert answers["handle_survived_every_reconnect"] is True
    # The Turkish answer is transcripts for a human, never a heuristic verdict.
    assert answers["turkish_output_transcripts"]
    assert "native speaker" in answers["turkish_verdict"]
    assert report["turkish"]["language_caveat"] is not None


def test_the_dry_run_writes_its_report_where_it_is_told(tmp_path: Path) -> None:
    out = tmp_path / "s4" / "report.json"
    assert main(["--dry-run", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["probe"] == "S4"


def test_with_no_key_it_refuses_cleanly_and_says_what_it_wants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(probe_live, "_keyring_key", lambda: None)
    # Even with one sitting in the environment: a process that silently picks up
    # a key from a shell is a process that bills somebody without asking.
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "keyring set jarvis gemini_api_key" in err
    assert "--dry-run" in err
