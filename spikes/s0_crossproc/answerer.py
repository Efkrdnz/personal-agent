#!/usr/bin/env python3
"""Spike S1, process B — the channel.

Forty lines of socket client standing in for the phone leg. It knows nothing
about Claude Code, never imports the SDK, and could equally be a Telegram bot,
a DTMF handler or a voice loop. That is the whole point: if this can answer a
plan-mode question, so can a phone call.

The answer-shaping rules it demonstrates are the ones that actually bite:
  - single-select  -> answers[question] is ONE label string
  - multiSelect    -> answers[question] is a LIST of label strings
  - "none of these"-> answers[question] is the user's own words, NOT the word
                      "Other" and not a label
  - every emitted label is resolved by LOCAL LOOKUP against the frozen options
    array by INDEX, so a reordered or invented label cannot be produced here

Usage:
    python answerer.py --socket /tmp/s.sock --pick 1
    python answerer.py --socket /tmp/s.sock --pick 1,3
    python answerer.py --socket /tmp/s.sock --free "Postgres, actually"
    python answerer.py --socket /tmp/s.sock            # interactive
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time

# Ordinal words, English and Turkish — parsed locally, before any model is
# consulted. "one and three" / "bir ve üç" must never need an API call.
ORDINALS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "bir": 1,
    "iki": 2,
    "üç": 3,
    "uc": 3,
    "dört": 4,
    "dort": 4,
    "beş": 5,
    "bes": 5,
}


def parse_picks(text: str, n_options: int) -> list[int]:
    """Spoken answer -> 1-based option indices. Pure, local, testable."""
    picks: list[int] = []
    for raw in text.replace(" ve ", " and ").replace(",", " and ").split(" and "):
        tok = raw.strip().lower().strip(".")
        if not tok:
            continue
        idx = int(tok) if tok.isdigit() else ORDINALS.get(tok)
        if idx and 1 <= idx <= n_options and idx not in picks:
            picks.append(idx)
    return picks


def connect(path: str, timeout: float = 60.0):
    deadline = time.time() + timeout
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            if time.time() > deadline:
                raise
            time.sleep(0.1)


def shape_answer(q: dict, picks: list[int], free: str | None) -> str | list[str]:
    """Build the value for answers[question]. Indices in, exact labels out."""
    if free is not None:
        return free
    options = q.get("options", [])
    labels = [options[i - 1]["label"] for i in picks]
    if not labels:
        raise SystemExit("no valid option selected")
    return labels if q.get("multiSelect") else labels[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--pick", help="1-based option numbers, e.g. '1' or '1,3'")
    ap.add_argument("--free", help="free-text answer ('none of these')")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sock = connect(args.socket)
    f = sock.makefile("rwb")
    f.write((json.dumps({"op": "next"}) + "\n").encode())
    f.flush()
    payload = json.loads(f.readline())

    answers: dict[str, str | list[str]] = {}
    for q in payload["questions"]:
        n = len(q.get("options", []))
        print(f"\n[{q.get('header', '?')}] {q['question']}", file=sys.stderr)
        for i, opt in enumerate(q.get("options", []), 1):
            print(f"  {i}. {opt['label']} — {opt.get('description', '')}", file=sys.stderr)
        print(f"  {n + 1}. None of these (say your own answer)", file=sys.stderr)

        if args.free is not None:
            answers[q["question"]] = shape_answer(q, [], args.free)
        elif args.pick:
            answers[q["question"]] = shape_answer(q, parse_picks(args.pick, n), None)
        else:
            said = input("> ")
            picks = parse_picks(said, n)
            answers[q["question"]] = shape_answer(q, picks, None if picks else said)

    reply = {"op": "answer", "request_id": payload["request_id"], "answers": answers}
    f.write((json.dumps(reply) + "\n").encode())
    f.flush()
    ack = json.loads(f.readline())
    out = {"answers": answers, "ack": ack, "tool_use_id": payload.get("tool_use_id")}
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
