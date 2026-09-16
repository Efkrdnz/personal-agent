#!/usr/bin/env python3
"""Spike S1, process A — the Claude Code driver and permission host.

Proves the load-bearing claim of the whole architecture: that a Claude Code
plan-mode question can be answered by a DIFFERENT PROCESS, and that a deferred
question survives the driver dying and being resumed.

This process:
  - runs one Claude Code job through ClaudeSDKClient in plan mode
  - hosts ``can_use_tool``; on AskUserQuestion it writes the question to SQLite,
    publishes it on a Unix socket, and BLOCKS until some other process answers
  - hosts a PreToolUse hook that logs every tool call and, under DEFER=1,
    asks the CLI to defer the question instead of blocking

Nothing here speaks, sees a microphone, or imports google.genai. That is the
point: this is the seam that the architecture says must exist from commit one.

Usage:
    python driver.py --scenario single --socket /tmp/s.sock --db /tmp/s.db
    DEFER=1 python driver.py --scenario single ...     # exits tool_deferred
    python driver.py --resume <session_id> ...         # re-fires the question
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
)

# ─── The prompts. Explicit, because the spike tests the plumbing, not the
#     model's propensity to ask. ────────────────────────────────────────────────

SCENARIOS = {
    "single": (
        "You are helping plan a tiny todo CLI. Before planning anything, call the "
        "AskUserQuestion tool exactly once with exactly ONE question asking how todos "
        "should be stored, offering exactly these three options with these exact "
        "labels: 'SQLite', 'JSON file', 'Plain text'. Do not set multiSelect. "
        "After you get the answer, reply with one short sentence naming the choice "
        "and stop. Do not call ExitPlanMode."
    ),
    "multi": (
        "You are helping plan a tiny todo CLI. Before planning anything, call the "
        "AskUserQuestion tool exactly once with exactly ONE question asking which "
        "features to include, with multiSelect set to true, offering exactly these "
        "four options with these exact labels: 'Due dates', 'Tags', 'Priorities', "
        "'Recurring'. After you get the answer, reply with one short sentence listing "
        "the choices and stop. Do not call ExitPlanMode."
    ),
    "freetext": (
        "You are helping plan a tiny todo CLI. Before planning anything, call the "
        "AskUserQuestion tool exactly once with exactly ONE question asking which "
        "database to use, offering exactly these two options with these exact labels: "
        "'SQLite', 'JSON file'. Do not set multiSelect. After you get the answer, "
        "reply with one short sentence naming exactly what the user chose, quoting it "
        "verbatim, and stop. Do not call ExitPlanMode."
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  kind      TEXT NOT NULL,
  tool_name TEXT,
  tool_use_id TEXT,
  payload   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id          TEXT PRIMARY KEY,
  tool_use_id TEXT NOT NULL,
  session_id  TEXT,
  state       TEXT NOT NULL,          -- pending | answered | consumed
  questions   TEXT NOT NULL,          -- the verbatim AskUserQuestion payload
  answers     TEXT,                   -- what some channel decided
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  answered_at TEXT
);
"""


def db_connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def log(con: sqlite3.Connection, kind: str, payload: dict, **cols: Any) -> None:
    con.execute(
        "INSERT INTO activity(kind, tool_name, tool_use_id, payload) VALUES (?,?,?,?)",
        (kind, cols.get("tool_name"), cols.get("tool_use_id"), json.dumps(payload)),
    )


# ─── The socket. This is the request bus in miniature: the driver holds a
#     pending question, any process may connect and answer it. ─────────────────


class QuestionBus:
    """A Unix socket carrying newline-delimited JSON.

    ``ask()`` parks a question and returns a future that some *other* process
    resolves. The driver never renders anything and never decides anything —
    exactly the split the phone layer will need.
    """

    def __init__(self, path: str, con: sqlite3.Connection):
        self.path = path
        self.con = con
        self._pending: dict[str, asyncio.Future] = {}
        self._payloads: dict[str, dict] = {}
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                op = msg.get("op")
                if op == "next":
                    # Block until a question exists, so the answerer may start first.
                    while not self._payloads:
                        await asyncio.sleep(0.05)
                    rid = next(iter(self._payloads))
                    writer.write(
                        (json.dumps({"request_id": rid, **self._payloads[rid]}) + "\n").encode()
                    )
                    await writer.drain()
                elif op == "answer":
                    rid = msg["request_id"]
                    fut = self._pending.get(rid)
                    self.con.execute(
                        "UPDATE requests SET state='answered', answers=?, "
                        "answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                        (json.dumps(msg["answers"]), rid),
                    )
                    if fut and not fut.done():
                        fut.set_result(msg["answers"])
                    writer.write((json.dumps({"ok": True}) + "\n").encode())
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def existing_answer(self, tool_use_id: str) -> dict | None:
        """Idempotent lookup — the whole reason a four-hour phone answer works.

        The answer may have been written while this process was dead. On resume
        the question re-fires and we return in microseconds.
        """
        row = self.con.execute(
            "SELECT answers FROM requests WHERE tool_use_id=? AND state='answered'",
            (tool_use_id,),
        ).fetchone()
        return json.loads(row["answers"]) if row else None

    async def ask(self, tool_use_id: str, session_id: str | None, questions: list[dict]) -> dict:
        rid = f"req_{uuid.uuid4().hex[:12]}"
        self.con.execute(
            "INSERT INTO requests(id, tool_use_id, session_id, state, questions) "
            "VALUES (?,?,?,'pending',?)",
            (rid, tool_use_id, session_id, json.dumps(questions)),
        )
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._payloads[rid] = {"questions": questions, "tool_use_id": tool_use_id}
        try:
            answers = await fut
        finally:
            self._pending.pop(rid, None)
            self._payloads.pop(rid, None)
        self.con.execute("UPDATE requests SET state='consumed' WHERE id=?", (rid,))
        return answers


# ─── The driver ──────────────────────────────────────────────────────────────


class Driver:
    def __init__(self, con: sqlite3.Connection, bus: QuestionBus, defer: bool):
        self.con = con
        self.bus = bus
        self.defer = defer
        self.session_id: str | None = None
        self.defer_requested: set[str] = set()

    async def _pre_tool(self, data: dict, tool_use_id: str | None, ctx) -> dict:
        """Activity log for every tool call, plus the defer experiment.

        Returning ``continue_`` rather than an allow decision matters: a hook
        ``allow`` does NOT short-circuit the deny and ask rules that follow it,
        and AskUserQuestion reaches can_use_tool regardless.
        """
        log(
            self.con,
            "tool.pre",
            {"input": data.get("tool_input", {})},
            tool_name=data.get("tool_name"),
            tool_use_id=tool_use_id or data.get("tool_use_id"),
        )
        if self.defer and data.get("tool_name") == "AskUserQuestion":
            tuid = tool_use_id or data.get("tool_use_id") or ""
            self.defer_requested.add(tuid)
            log(self.con, "tool.defer_requested", {"tool_use_id": tuid})
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "defer",
                    "permissionDecisionReason": "user is away; ask them out of band",
                }
            }
        return {"continue_": True}

    async def _can_use_tool(self, tool_name: str, input_data: dict, context) -> Any:
        tuid = getattr(context, "tool_use_id", None) or ""

        if tool_name != "AskUserQuestion":
            log(self.con, "permission.auto_allow", {"tool": tool_name}, tool_name=tool_name)
            return PermissionResultAllow()

        questions = input_data.get("questions", [])
        log(
            self.con,
            "question.raised",
            {"questions": questions},
            tool_name=tool_name,
            tool_use_id=tuid,
        )

        # Was this already answered while we were dead? (The resume path.)
        answers = self.bus.existing_answer(tuid)
        if answers is not None:
            log(self.con, "question.answer_replayed", {"answers": answers}, tool_use_id=tuid)
        else:
            answers = await self.bus.ask(tuid, self.session_id, questions)
            log(self.con, "question.answered", {"answers": answers}, tool_use_id=tuid)

        if not isinstance(answers, dict) or not answers:
            return PermissionResultDeny(message="no answer supplied")

        # THE payload shape. `response` is never set alongside `answers`: when
        # response is present Claude receives "The user responded: ..." instead
        # of the per-question answer list, silently discarding the answers.
        return PermissionResultAllow(updated_input={**input_data, "answers": answers})

    def options(self, *, resume: str | None) -> ClaudeAgentOptions:
        ident = {"resume": resume} if resume else {}
        return ClaudeAgentOptions(
            permission_mode="plan",  # NEVER dontAsk: it denies AskUserQuestion outright
            # AskUserQuestion is deliberately NOT in allowed_tools. The SDK warns
            # that "an allowed_tools entry that allows a whole tool auto-approves
            # it before the callback is consulted" — listing the very tool the
            # permission host exists to serve is how you silently lose the host.
            # Measured: with it listed the callback still fired, but the warning
            # is right about the mechanism and settings-file allow rules can
            # shadow it invisibly. Don't rely on it being benign.
            disallowed_tools=["Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task"],
            can_use_tool=self._can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool])]},
            max_turns=6,
            **ident,
        )

    async def run(self, prompt: str | None, *, resume: str | None = None) -> dict:
        out: dict[str, Any] = {"text": [], "stop_reason": None, "deferred": None}
        async with ClaudeSDKClient(options=self.options(resume=resume)) as client:
            await client.query(prompt if prompt else "Continue.")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            out["text"].append(block.text.strip())
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id
                    out["session_id"] = msg.session_id
                    out["stop_reason"] = msg.stop_reason
                    out["is_error"] = msg.is_error
                    out["total_cost_usd"] = msg.total_cost_usd
                    if msg.deferred_tool_use:
                        d = msg.deferred_tool_use
                        out["deferred"] = {"id": d.id, "name": d.name, "input": d.input}
        log(self.con, "job.finished", out)
        return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="single")
    ap.add_argument("--socket", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--resume", default=None, help="resume this session instead of starting one")
    ap.add_argument("--out", default=None, help="write the result JSON here")
    args = ap.parse_args()

    con = db_connect(args.db)
    bus = QuestionBus(args.socket, con)
    await bus.start()
    driver = Driver(con, bus, defer=os.environ.get("DEFER") == "1")
    try:
        result = await driver.run(
            None if args.resume else SCENARIOS[args.scenario], resume=args.resume
        )
    finally:
        await bus.stop()

    payload = json.dumps(result, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(payload)
    print(payload)
    return 1 if result.get("is_error") else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
