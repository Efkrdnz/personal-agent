#!/usr/bin/env python3
"""Turn the Windows build's logs into GitHub annotations.

    python packaging/ci_annotate.py failure ci-logs               # one ::error per log/report
    python packaging/ci_annotate.py junit ci-logs/pytest.xml      # one ::error per failed test
    python packaging/ci_annotate.py notice selftest ci-logs/selftest.json

WHY ANNOTATIONS. The Windows job is debugged from outside GitHub: the check-run
API returns annotations, and the raw step logs are not reachable from there. So
every step tees its output into ``ci-logs/``, and when anything fails this
script re-emits the end of each file as an ``::error``. On success the
selftest's JSON goes out as a ``::notice``, so a green run still says what the
built exe found inside itself.

THE LIMITS ARE GITHUB'S. The runner truncates an annotation's message to its
first 4096 characters — the START, which for a log is the half nobody needs —
and keeps only ten annotations of each kind per step. So each message is cut
here, from the top, to fit; a long JSON report is compacted to one line rather
than tailed; and a file or test past the tenth is named in the last annotation
instead of being silently dropped.

Standard library only: this runs when the install step may be what failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_CHARS",
    "MAX_PER_STEP",
    "command",
    "failure",
    "junit",
    "main",
    "notice",
    "report",
    "report_chunks",
    "tail",
]

#: Below the runner's 4096, leaving room for a marker line.
MAX_CHARS = 4000
#: The runner keeps ten errors (and ten notices) per step and drops the rest.
MAX_PER_STEP = 10
#: Lines taken from the end of each log before the size limit applies.
TAIL_LINES = 60
#: One runaway line (a minified JSON blob, a progress bar) must not be the whole budget.
_LINE_CHARS = 400
_SUFFIXES = (".log", ".json", ".txt")


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def command(kind: str, title: str, message: str) -> str:
    """One workflow command line: ``::error title=<title>::<message>``, escaped."""
    return f"::{kind} title={_escape_property(title)}::{_escape_data(message)}"


def tail(text: str, lines: int = TAIL_LINES, limit: int = MAX_CHARS) -> str:
    """The last ``lines`` lines that fit in ``limit`` characters, newest kept."""
    rows = [
        r if len(r) <= _LINE_CHARS else r[: _LINE_CHARS - 1] + "…"
        for r in text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").split("\n")
    ][-lines:]
    kept: list[str] = []
    size = 0
    for row in reversed(rows):
        if size + len(row) + 1 > limit:
            break
        kept.append(row)
        size += len(row) + 1
    return "\n".join(reversed(kept))


def _read(path: Path) -> str:
    # utf-8-sig: Windows PowerShell 5 tees with a BOM, PowerShell 7 without.
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _dumps(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def report(obj: object, limit: int = MAX_CHARS) -> str:
    """A JSON report on one line, failures first, shrunk to fit ``limit``.

    The selftest's report is ``{check: {"ok": bool, "detail": str, ...}}``. A
    pretty-printed report's last 60 lines are its last few checks, which is
    the wrong end; and cut at 4 KB, the failing check is as likely as not the
    part that falls off. So failures go first, and when the whole report does
    not fit, a passing check shrinks to ``true`` before a failure loses a word.
    """
    checks = isinstance(obj, dict) and all(isinstance(v, dict) and "ok" in v for v in obj.values())
    if not checks:
        text = _dumps(obj)
        return text if len(text) <= limit else text[: limit - 14] + " …(truncated)"
    bad = {k: v for k, v in obj.items() if not v.get("ok")}
    good = {k: v for k, v in obj.items() if v.get("ok")}
    for shrunk in (
        {**bad, **good},
        {**bad, **dict.fromkeys(good, True)},
        {
            **{k: {**v, "detail": str(v.get("detail", ""))[:300]} for k, v in bad.items()},
            **dict.fromkeys(good, True),
        },
    ):
        text = _dumps(shrunk)
        if len(text) <= limit:
            return text
    return text[: limit - 14] + " …(truncated)"


def _body(path: Path) -> str:
    text = _read(path)
    if path.suffix == ".json":
        try:
            return report(json.loads(text))
        except ValueError:
            return tail(text) or "(not valid JSON, and empty)"
    return tail(text) or "(empty)"


def _capped(items: list[tuple[str, str]], kind: str, what: str) -> list[str]:
    if len(items) > MAX_PER_STEP:
        rest = [title for title, _ in items[MAX_PER_STEP - 1 :]]
        items = items[: MAX_PER_STEP - 1] + [
            (f"{len(rest)} more {what}", "Not annotated (ten per step): " + ", ".join(rest))
        ]
    return [command(kind, title, body) for title, body in items]


def failure(log_dir: Path) -> list[str]:
    """One ``::error`` per captured log or report, in name order."""
    files = sorted(p for p in log_dir.rglob("*") if p.is_file() and p.suffix.lower() in _SUFFIXES)
    if not files:
        return [command("error", "ci-logs", f"no logs were captured in {log_dir}")]
    return _capped([(p.relative_to(log_dir).as_posix(), _body(p)) for p in files], "error", "files")


def junit(xml_path: Path) -> list[str]:
    """``::error`` lines for the failed and erroring tests in a pytest ``--junitxml`` report.

    Up to ten failures, one annotation each with the end of its traceback.
    Past ten, the ten-per-step limit would leave the rest as bare names, and
    a name is not a diagnosis — so every failure is cut to its digest (the
    ``E`` lines and where they were raised) and the digests are packed
    several to an annotation.
    """
    if not xml_path.is_file():
        return []
    items: list[tuple[str, str, str]] = []
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        for bad in (*case.findall("failure"), *case.findall("error")):
            where = ".".join(filter(None, (case.get("classname"), case.get("name"))))
            head = (bad.get("message") or "").strip()
            items.append((f"{bad.tag}: {where}", head, bad.text or ""))
    if len(items) <= MAX_PER_STEP:
        return [
            command("error", title, tail(f"{head}\n\n{text}".strip(), lines=40) or title)
            for title, head, text in items
        ]
    return _packed([_digest(*item) for item in items], len(items))


#: How much of one failure a packed annotation carries.
_DIGEST_CHARS = 900
_DIGEST_E_LINES = 8


def _digest(title: str, head: str, text: str) -> str:
    rows = text.replace("\r\n", "\n").split("\n")
    errors = [r.rstrip() for r in rows if r.startswith("E ")][:_DIGEST_E_LINES]
    # pytest's "path:line: ExceptionType" lines say where it was raised.
    raised = [r.strip() for r in rows if re.match(r"^\S.*:\d+: \w", r)][-2:]
    first = head.split("\n", 1)[0]
    body = "\n".join([f"## {title}", first[:300], *errors, *raised])
    return body if len(body) <= _DIGEST_CHARS else body[: _DIGEST_CHARS - 1] + "…"


def _packed(digests: list[str], total: int) -> list[str]:
    groups: list[list[str]] = [[]]
    for d in digests:
        if groups[-1] and len("\n\n".join([*groups[-1], d])) > MAX_CHARS:
            groups.append([])
        groups[-1].append(d)
    if len(groups) > MAX_PER_STEP:
        rest = [
            d.split("\n", 1)[0].removeprefix("## ") for g in groups[MAX_PER_STEP - 1 :] for d in g
        ]
        groups = [*groups[: MAX_PER_STEP - 1], ["Not annotated (ten per step): " + ", ".join(rest)]]
    out = []
    shown = 0
    for g in groups:
        first = shown + 1
        shown += len(g)
        out.append(
            command("error", f"failed tests {first}-{shown} of {total}", "\n\n".join(g)[:MAX_CHARS])
        )
    return out


def report_chunks(obj: dict[str, Any], limit: int = MAX_CHARS) -> list[str]:
    """A check report split into one-line JSON pieces of at most ``limit`` characters.

    For a GREEN run's notice, where nothing is failing and every detail is the
    point (which PortAudio, which keyring backend, how many bytes SAPI spoke):
    several notices rather than one with the details squeezed out.
    """
    ordered = sorted(obj.items(), key=lambda kv: bool(isinstance(kv[1], dict) and kv[1].get("ok")))
    chunks: list[dict[str, Any]] = [{}]
    for key, value in ordered:
        if len(_dumps({key: value})) > limit and isinstance(value, dict):
            value = {**value, "detail": str(value.get("detail", ""))[:300]}
        if chunks[-1] and len(_dumps({**chunks[-1], key: value})) > limit:
            chunks.append({})
        chunks[-1][key] = value
    return [_dumps(c) for c in chunks if c]


def notice(title: str, path: Path, *, lines: int | None = None) -> list[str]:
    """``::notice`` lines for one file: its JSON in as many pieces as it takes, or its tail."""
    if not path.is_file():
        return [command("warning", title, f"{path} was not written")]
    if lines or path.suffix != ".json":
        return [command("notice", title, tail(_read(path), lines=lines or TAIL_LINES))]
    try:
        obj = json.loads(_read(path))
    except ValueError:
        return [command("notice", title, tail(_read(path)))]
    if not isinstance(obj, dict):
        return [command("notice", title, report(obj))]
    pieces = report_chunks(obj)[:MAX_PER_STEP]
    if len(pieces) == 1:
        return [command("notice", title, pieces[0])]
    return [command("notice", f"{title} ({i}/{len(pieces)})", p) for i, p in enumerate(pieces, 1)]


def _emit(lines: Iterable[str]) -> int:
    for line in lines:
        print(line, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="what", required=True)
    f = sub.add_parser("failure", help="an ::error per log and report in a directory")
    f.add_argument("dir", type=Path)
    j = sub.add_parser("junit", help="an ::error per failed test in a pytest junit report")
    j.add_argument("report", type=Path)
    n = sub.add_parser("notice", help="a ::notice with one file's content")
    n.add_argument("title")
    n.add_argument("path", type=Path)
    n.add_argument("--lines", type=int, default=None, help="only the last N lines")
    args = ap.parse_args(argv)

    if args.what == "failure":
        return _emit(failure(args.dir))
    if args.what == "junit":
        return _emit(junit(args.report))
    return _emit(notice(args.title, args.path, lines=args.lines))


if __name__ == "__main__":
    sys.exit(main())
