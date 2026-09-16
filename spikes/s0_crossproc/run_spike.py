#!/usr/bin/env python3
"""Spike S1 orchestrator — runs every scenario and records what actually happened.

Four scenarios, each a separate pair of OS processes:

  1. single     a single-select plan-mode question answered by another process
  2. multi      multiSelect, answered with a LIST of labels
  3. freetext   "none of these" — the user's own words, not a label
  4. defer      PreToolUse returns defer -> the driver EXITS -> it is KILLED ->
                a gap passes -> a fresh process resumes -> the question re-fires
                and the answer, written while the driver was dead, lands

Scenario 4 is the one that matters. It is "Jarvis calls you while you're out"
in miniature, and if it fails the phone layer needs a different design.

    python run_spike.py            # all scenarios
    python run_spike.py --only defer --gap 600
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
PY = sys.executable
WORK = Path(os.environ.get("SPIKE_WORK", "/tmp/jarvis-spike"))


def fresh(name: str) -> tuple[str, str]:
    WORK.mkdir(parents=True, exist_ok=True)
    sock, db = WORK / f"{name}.sock", WORK / f"{name}.db"
    for p in (sock, db, Path(f"{db}-wal"), Path(f"{db}-shm")):
        p.unlink(missing_ok=True)
    return str(sock), str(db)


def answers_of(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM requests")]
    finally:
        con.close()


def kinds_of(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT kind FROM activity ORDER BY seq")]
    finally:
        con.close()


def run_pair(name: str, scenario: str, answerer_args: list[str], timeout: int = 300) -> dict:
    """One driver process, one answerer process, both real."""
    sock, db = fresh(name)
    out = WORK / f"{name}.driver.json"
    ans = subprocess.Popen(
        [PY, str(HERE / "answerer.py"), "--socket", sock, *answerer_args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    drv = subprocess.run(
        [PY, str(HERE / "driver.py"), "--scenario", scenario, "--socket", sock,
         "--db", db, "--out", str(out)],
        capture_output=True, text=True, timeout=timeout,
    )
    a_out, a_err = ans.communicate(timeout=30)
    result = json.loads(out.read_text()) if out.exists() else {}
    return {
        "scenario": scenario,
        "driver_rc": drv.returncode,
        "driver_stderr_tail": drv.stderr.strip().splitlines()[-3:],
        "result": result,
        "answerer_rendered": a_err.strip(),
        "answerer_sent": json.loads(a_out) if a_out.strip().startswith("{") else a_out,
        "requests": answers_of(db),
        "activity": kinds_of(db),
        "db": db,
    }


def run_defer(gap_seconds: int, timeout: int = 300) -> dict:
    """The one that proves the phone layer is possible.

    Phase 1  DEFER=1. The hook asks the CLI to defer; the driver should exit
             rather than block, surrendering stop_reason and a deferred payload.
    Phase 2  SIGKILL the process group, to be certain nothing survived.
    Phase 3  Answer while nobody is listening — writing straight to the DB,
             exactly as a Telegram bot or a phone leg would.
    Phase 4  A FRESH process resumes the session. The question must re-fire and
             the already-written answer must be replayed without asking again.
    """
    sock, db = fresh("defer")
    out1, out2 = WORK / "defer.p1.json", WORK / "defer.p2.json"
    phases: dict = {}

    env = {**os.environ, "DEFER": "1"}
    p = subprocess.Popen(
        [PY, str(HERE / "driver.py"), "--scenario", "single", "--socket", sock,
         "--db", db, "--out", str(out1)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        o, e = p.communicate(timeout=timeout)
        phases["p1_exited_on_its_own"] = True
    except subprocess.TimeoutExpired:
        # Defer was ignored and it blocked instead — itself a finding.
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        o, e = p.communicate()
        phases["p1_exited_on_its_own"] = False
    phases["p1_rc"] = p.returncode
    phases["p1_result"] = json.loads(out1.read_text()) if out1.exists() else {}
    phases["p1_stderr_tail"] = (e or "").strip().splitlines()[-3:]

    # Phase 2 — make sure it is really gone.
    with __import__("contextlib").suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    phases["p2_process_killed"] = True

    session_id = phases["p1_result"].get("session_id")
    deferred = phases["p1_result"].get("deferred")
    phases["deferred_payload_present"] = deferred is not None

    if not session_id:
        phases["verdict"] = "FAIL: no session_id to resume"
        return phases

    # Phase 3 — answer out of band, with no driver alive to receive it.
    # isolation_level=None: autocommit. Without it sqlite3 opens a deferred
    # transaction and con.close() silently rolls the offline answer back — which
    # looks exactly like "resume lost the answer" and is not.
    con = sqlite3.connect(db, isolation_level=None)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None and deferred:
        # Defer fired before can_use_tool, so no request row exists yet. Seed one
        # from the deferred payload — which is precisely how the away-path works:
        # the question is known from deferred_tool_use.input, not from the host.
        qs = deferred["input"].get("questions", [])
        answer = {qs[0]["question"]: qs[0]["options"][1]["label"]} if qs else {}
        con.execute(
            "INSERT INTO requests(id, tool_use_id, session_id, state, questions, answers,"
            " answered_at) VALUES ('req_offline', ?, ?, 'answered', ?, ?,"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (deferred["id"], session_id, json.dumps(qs), json.dumps(answer)),
        )
        phases["answered_offline"] = answer
        phases["answer_keyed_on_tool_use_id"] = deferred["id"]
    elif row is not None:
        qs = json.loads(row["questions"])
        answer = {qs[0]["question"]: qs[0]["options"][1]["label"]} if qs else {}
        con.execute(
            "UPDATE requests SET state='answered', answers=?,"
            " answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (json.dumps(answer), row["id"]),
        )
        phases["answered_offline"] = answer
        phases["answer_keyed_on_tool_use_id"] = row["tool_use_id"]
    con.close()

    # Phase 4 — the gap, then a brand-new process.
    phases["gap_seconds"] = gap_seconds
    time.sleep(gap_seconds)

    r = subprocess.run(
        [PY, str(HERE / "driver.py"), "--socket", sock, "--db", db,
         "--resume", session_id, "--out", str(out2)],
        capture_output=True, text=True, timeout=timeout,
    )
    phases["p4_rc"] = r.returncode
    phases["p4_result"] = json.loads(out2.read_text()) if out2.exists() else {}
    phases["p4_stderr_tail"] = r.stderr.strip().splitlines()[-3:]
    phases["activity"] = kinds_of(db)
    phases["requests"] = answers_of(db)
    phases["answer_replayed"] = "question.answer_replayed" in phases["activity"]
    phases["db"] = db
    return phases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["single", "multi", "freetext", "defer"])
    ap.add_argument("--gap", type=int, default=60, help="seconds between kill and resume")
    ap.add_argument("--out", default=str(WORK / "results.json"))
    args = ap.parse_args()

    results: dict = {}
    want = [args.only] if args.only else ["single", "multi", "freetext", "defer"]

    if "single" in want:
        print("→ scenario 1: single-select, answered by another process", file=sys.stderr)
        results["single"] = run_pair("single", "single", ["--pick", "1"])
    if "multi" in want:
        print("→ scenario 2: multiSelect, answered with a list", file=sys.stderr)
        results["multi"] = run_pair("multi", "multi", ["--pick", "one and three"])
    if "freetext" in want:
        print("→ scenario 3: 'none of these', free text", file=sys.stderr)
        results["freetext"] = run_pair(
            "freetext", "freetext", ["--free", "Postgres, actually"]
        )
    if "defer" in want:
        print(f"→ scenario 4: defer, kill, {args.gap}s gap, resume", file=sys.stderr)
        results["defer"] = run_defer(args.gap)

    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results, indent=2, default=str)[:400])
    print(f"\nfull results: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
