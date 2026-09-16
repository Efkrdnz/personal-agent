#!/usr/bin/env python3
"""Stage 2, live — the headline feature end to end, minus the microphone.

This runs the real Claude Code CLI through the real spine, with the plan-mode
answer supplied by a DIFFERENT OS PROCESS via the requests table. Everything
between the spoken words and the built code is exercised:

    a job row  ->  `python -m jarvis.cc` in its own process  ->  Claude enters
    plan mode and asks  ->  the permission host writes a request row and blocks
    ->  THIS process (the stand-in for a voice loop, a Telegram bot or a phone
        leg) reads the frozen option array and answers by INDEX
    ->  the host resolves the index to a verbatim label, validates it against
        the frozen array, and returns updated_input
    ->  Claude builds on the choice  ->  the activity log holds the whole thing

Spike S1 proved the CLI mechanics. This proves the mechanics still work when
routed through jobs, requests, bus, ledger and the permission host — which is a
different claim, and the one stage 2 actually rests on.

    python spikes/s2_live_slice/run_slice.py
    python spikes/s2_live_slice/run_slice.py --scenario multi --keep

Costs one short Claude Code turn (~$0.05). Requires working Claude Code auth.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from jarvis import bus, db, jobs, ledger  # noqa: E402
from jarvis import requests as rq  # noqa: E402
from jarvis.cc import narrate  # noqa: E402

PROMPTS = {
    "single": (
        "You are planning a tiny todo CLI. Before planning anything else, call the "
        "AskUserQuestion tool exactly once with exactly ONE question asking how todos "
        "should be stored, offering exactly these three options with these exact labels: "
        "'SQLite', 'JSON file', 'Plain text'. Do not set multiSelect. After you get the "
        "answer, reply with one short sentence naming the choice and stop. Do not call "
        "ExitPlanMode."
    ),
    "multi": (
        "You are planning a tiny todo CLI. Before planning anything else, call the "
        "AskUserQuestion tool exactly once with exactly ONE question asking which features "
        "to include, with multiSelect set to true, offering exactly these four options with "
        "these exact labels: 'Due dates', 'Tags', 'Priorities', 'Recurring'. After you get "
        "the answer, reply with one short sentence listing the choices and stop. Do not call "
        "ExitPlanMode."
    ),
}
PICKS = {"single": [1], "multi": [1, 3]}


def wait_for_pending(con, job_id: str, timeout: float = 240.0) -> rq.Request:
    """Poll for the question, exactly as a channel process would."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for r in rq.open_requests(con):
            if r.job_id == job_id and r.state == "pending":
                return r
        time.sleep(0.2)
    raise TimeoutError("no pending request appeared — the driver never asked")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(PROMPTS), default="single")
    ap.add_argument("--keep", action="store_true", help="keep the temp DB for inspection")
    ap.add_argument("--out", default="/tmp/jarvis-spike/s2_slice.json")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="jarvis-s2-"))
    dbpath = work / "jarvis.db"
    cwd = work / "project"
    cwd.mkdir()

    con = db.open_db(dbpath)
    job = jobs.create_job(
        con,
        kind="claude_code",
        title="the todo app build",
        created_by="desk",
        cwd=str(cwd),
        permission_mode="plan",
    )
    report: dict = {"scenario": args.scenario, "job_id": job.id, "db": str(dbpath)}
    print(f"job {job.id} in {cwd}", file=sys.stderr)

    # ── the driver, in its own OS process ────────────────────────────────────
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jarvis.cc",
            "--job-id",
            job.id,
            "--db",
            str(dbpath),
            "--prompt",
            PROMPTS[args.scenario],
            "--channel",
            "desk",
        ],
        cwd=str(REPO),
        env={**os.environ, "JARVIS_DB": str(dbpath)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # ── this process is the channel. It never imports the SDK. ───────────────
    answerer = db.connect(dbpath)
    try:
        req = wait_for_pending(answerer, job.id)
    except TimeoutError as e:
        proc.kill()
        out, err = proc.communicate()
        report["error"] = str(e)
        report["driver_stderr"] = err[-2000:]
        print(json.dumps(report, indent=2))
        return 1

    # What the user would have heard, generated locally from the frozen payload.
    raw = json.loads(req.payload) if isinstance(req.payload, str) else req.payload
    questions = narrate.questions_of(raw)
    spoken = [ln.text for ln in narrate.script(questions)]
    report["spoken_to_the_user"] = spoken
    print("\n".join(f"  {s}" for s in spoken), file=sys.stderr)

    picks = PICKS[args.scenario]
    answer = narrate.answer(questions, picks)
    report["picked_indices"] = picks
    report["answer_sent"] = answer

    won = rq.answer_request(answerer, req.id, answer=answer, answered_by="cli", answer_mode="voice")
    report["answer_accepted"] = won
    print(f"answered by index {picks} -> {answer}", file=sys.stderr)

    out, err = proc.communicate(timeout=300)
    report["driver_rc"] = proc.returncode
    report["driver_stderr_tail"] = err.strip().splitlines()[-5:]

    # ── what the spine recorded ──────────────────────────────────────────────
    final = jobs.get(con, job.id)
    report["job_state"] = final.state
    report["stop_reason"] = final.stop_reason
    report["claude_said"] = (final.result_summary or "").strip()[:400]
    report["events"] = [r[0] for r in con.execute("SELECT kind FROM events ORDER BY seq")]
    report["chain_verifies"] = bus.verify_chain(con) is None
    st = ledger.status(con, "today")
    report["spend_spoken"] = ledger.spoken_status(st)

    # ── the assertions that make this a proof rather than a demo ─────────────
    labels = [o["label"] for q in questions for o in q.get("options", [])]
    chosen = [labels[i - 1] for i in picks]
    said = report["claude_said"].lower()
    report["chosen_labels"] = chosen
    report["claude_used_the_choice"] = all(c.lower() in said for c in chosen)
    report["verdict"] = (
        "PASS — a live plan-mode question was answered by another process through the spine"
        if report["claude_used_the_choice"] and won and proc.returncode == 0
        else "FAIL"
    )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    if not args.keep:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    return 0 if report["claude_used_the_choice"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
