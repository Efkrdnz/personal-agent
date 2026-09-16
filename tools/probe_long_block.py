#!/usr/bin/env python3
"""Probe S9(c) — can ``can_use_tool`` block for a long time without being reaped?

The at-desk flow assumes Jarvis can raise a plan-mode question, wait for the user
to wander back from the kitchen, and still deliver the answer into the same live
session. The architecture's honest-risks list flags this as untested:

    "The permission callback blocking for 90 minutes over the SDK's stdio control
    protocol is untested. If the CLI reaps a long-pending callback, the at-desk
    flow silently becomes defer-only — and since defer is itself best-effort,
    that combination would be a real problem discovered late."

This settles it by measurement. It holds the callback open for N seconds, then
answers, and records whether the answer actually landed.

    python tools/probe_long_block.py --seconds 300          # validate the harness
    python tools/probe_long_block.py --seconds 5400 &       # the real 90-minute run

Writes JSON to --out. Costs one short Claude Code turn plus the wait.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
)

PROMPT = (
    "Call the AskUserQuestion tool exactly once with exactly ONE question asking which "
    "database to use, offering exactly these two options with these exact labels: "
    "'SQLite', 'Postgres'. Do not set multiSelect. After you get the answer, reply with "
    "one short sentence naming the choice and stop. Do not call ExitPlanMode."
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5400.0, help="how long to hold the callback")
    ap.add_argument("--out", default="/tmp/jarvis-spike/long_block.json")
    args = ap.parse_args()

    result: dict[str, Any] = {"requested_block_s": args.seconds}
    state: dict[str, Any] = {"asked_at": None, "released_at": None, "held_s": None}

    async def can_use_tool(tool_name: str, input_data: dict, context) -> Any:
        if tool_name != "AskUserQuestion":
            return PermissionResultAllow()

        state["asked_at"] = time.monotonic()
        questions = input_data.get("questions", [])
        # Hold it open. This is the whole probe: everything else is bookkeeping.
        await asyncio.sleep(args.seconds)
        state["released_at"] = time.monotonic()
        state["held_s"] = state["released_at"] - state["asked_at"]

        answers = {}
        for q in questions:
            opts = q.get("options", [])
            if opts:
                answers[q["question"]] = opts[-1]["label"]  # 'Postgres', distinguishable
        state["answers_sent"] = answers
        return PermissionResultAllow(updated_input={**input_data, "answers": answers})

    opts = ClaudeAgentOptions(
        permission_mode="plan",
        disallowed_tools=["Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task"],
        can_use_tool=can_use_tool,
        max_turns=6,
    )

    started = time.monotonic()
    texts: list[str] = []
    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(PROMPT)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            texts.append(b.text.strip())
                elif isinstance(msg, ResultMessage):
                    result["stop_reason"] = msg.stop_reason
                    result["is_error"] = msg.is_error
                    result["total_cost_usd"] = msg.total_cost_usd
                    result["session_id"] = msg.session_id
    except Exception as e:  # noqa: BLE001 — the failure IS the finding
        result["exception"] = f"{type(e).__name__}: {e}"

    result["wall_s"] = time.monotonic() - started
    result["texts"] = texts
    result.update(state)

    # The verdict, stated in the terms the architecture needs.
    answered = state.get("answers_sent") or {}
    chosen = next(iter(answered.values()), None)
    landed = bool(chosen) and any(chosen.lower() in t.lower() for t in texts)
    result["callback_was_reaped"] = state["released_at"] is None or "exception" in result
    result["answer_landed_after_the_block"] = landed
    result["verdict"] = (
        "PASS — the callback held and the answer landed"
        if landed and not result["callback_was_reaped"]
        else "FAIL — a long block is not survivable; the at-desk flow must use defer"
    )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))
    return 0 if landed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
