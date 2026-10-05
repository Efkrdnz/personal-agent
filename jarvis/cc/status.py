"""Is Claude Code installed here, and signed in — asked of the CLI itself.

"Am I logged in to Claude Code?" was answered with "I can't see your terminal",
while the CLI that the driver runs can say exactly that: ``claude auth status
--json``. This module asks it, hidden, with a timeout, and hands back a small
dict of ALLOW-LISTED facts.

WHAT THE CLI SAYS, measured against 2.1.273 (the commands research, section 3):

* exit 0 and ``{"loggedIn": true, "authMethod": ...}`` when credentials are
  configured; ``authMethod`` is ``claude.ai``, ``api_key``, ``oauth_token`` or
  ``third_party`` (with ``apiProvider`` ``bedrock`` / ``vertex``);
* exit **1** and ``{"loggedIn": false, "authMethod": "none", ...}`` when not —
  so a non-zero exit is an answer here, not a failure;
* a claude.ai login adds ``email``, ``orgId``, ``orgName``, ``subscriptionType``;
* an API key adds ``apiKeySource`` (``ANTHROPIC_API_KEY``), and the key itself
  is never printed.

"Logged in" means credentials are configured: a fabricated token reports
``loggedIn: true``. The check is local, so the sentence built from it must not
claim the credentials work.

NEVER DEFAULTS TO YES. A missing CLI, a timeout, a crash or output that does not
parse gives ``logged_in`` None with an ``error``; only a ``loggedIn`` that parsed
as a boolean, from an exit code that agrees with it, is believed.

ALLOW-LISTED. ``orgId``, ``configDirectory`` and ``projectsDirectory`` are
dropped here, and the raw output is never logged: what leaves this function is
what a sentence about sign-in needs, and nothing a log reader should not see.

THE ENVIRONMENT IS THE DRIVER'S: ``os.environ`` minus ``CLAUDECODE``, as the
SDK runs the CLI, so the answer is about the credentials a build would use.
Standard library only; ``jarvis.cc`` imports nothing, so this needs no SDK.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

__all__ = ["FIELDS", "TIMEOUT_S", "probe"]

#: Everything a caller may ever see. Keep it this short.
FIELDS: tuple[str, ...] = (
    "installed",
    "version",
    "logged_in",
    "auth_method",
    "provider",
    "subscription",
    "org_name",
    "email",
    "key_source",
    "error",
)

#: Measured at about a quarter of a second; a CLI that takes fifteen is wedged.
TIMEOUT_S = 15.0

_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.]+)?")
#: Field values are short identifiers or names. Anything else is not trusted
#: into a sentence.
_SAFE = re.compile(r"[\w .,@+\-'&()/]{1,80}")


def probe(
    cli: Sequence[str] | str | None,
    *,
    run: Callable[..., Any] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = TIMEOUT_S,
    platform: str | None = None,
) -> dict[str, Any]:
    """Ask the CLI. Never raises; never says signed in unless the CLI did."""
    argv = [cli] if isinstance(cli, str) else list(cli or ())
    if not argv or not argv[0]:
        return _out(installed=False, logged_in=None, error="Claude Code is not installed")
    call = run or _run
    base = os.environ if env is None else env
    child_env = {k: v for k, v in base.items() if k != "CLAUDECODE"}
    plat = platform or sys.platform

    try:
        version = call([*argv, "--version"], env=child_env, timeout=timeout_s, platform=plat)
    except FileNotFoundError:
        return _out(installed=False, logged_in=None, error="Claude Code is not installed")
    except subprocess.TimeoutExpired:
        return _out(installed=None, logged_in=None, error="Claude Code did not answer in time")
    except (OSError, subprocess.SubprocessError) as exc:
        return _out(
            installed=None, logged_in=None, error=f"could not start it ({type(exc).__name__})"
        )
    found = _VERSION.search(_text(getattr(version, "stdout", b"")))
    facts: dict[str, Any] = {"installed": True, "version": found.group() if found else None}

    try:
        status = call(
            [*argv, "auth", "status", "--json"], env=child_env, timeout=timeout_s, platform=plat
        )
    except subprocess.TimeoutExpired:
        return _out(**facts, logged_in=None, error="the sign-in check did not answer in time")
    except (OSError, subprocess.SubprocessError) as exc:
        return _out(
            **facts, logged_in=None, error=f"the sign-in check failed ({type(exc).__name__})"
        )

    data = _json(_text(getattr(status, "stdout", b""))) or {}
    logged = data.get("loggedIn")
    code = getattr(status, "returncode", None)
    if not isinstance(logged, bool):
        return _out(**facts, logged_in=None, error="the sign-in check's answer could not be read")
    if logged and code != 0:
        # Says yes and fails: believe neither.
        return _out(
            **facts, logged_in=None, error=f"the sign-in check contradicted itself (exit {code})"
        )
    return _out(
        **facts,
        logged_in=logged,
        auth_method=_safe(data.get("authMethod")),
        provider=_safe(data.get("apiProvider")),
        subscription=_safe(data.get("subscriptionType")),
        org_name=_safe(data.get("orgName")),
        email=_safe(data.get("email")),
        key_source=_safe(data.get("apiKeySource")),
    )


def _run(argv: list[str], *, env: dict[str, str], timeout: float, platform: str) -> Any:
    return subprocess.run(  # noqa: S603 - a fixed argv, never a shell
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
        # The app is windowed: without this claude.exe flashes a console.
        creationflags=0x08000000 if platform == "win32" else 0,
    )


def _text(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _json(text: str) -> dict[str, Any] | None:
    """The one JSON object in the output, tolerating a warning line around it."""
    text = text.strip()
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _safe(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and _SAFE.fullmatch(value) else None


def _out(**facts: Any) -> dict[str, Any]:
    """Every field present, in the allow-list's order; nothing else."""
    return {name: facts.get(name) for name in FIELDS}
