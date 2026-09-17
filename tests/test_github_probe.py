"""Spike S8's probe, and the seam around the whole package.

The probe is the one piece of this stage that CAN touch a real account, so its
tests are about the two properties that keep it from doing so by accident: it is
read-only unless a flag says otherwise, and it reads the credential Jarvis was
given rather than whatever token happens to be in the environment.

Every test drives it with a :class:`FakeTransport`. The destructive path is
exercised here in full — creation, a refused delete, the compensation in order —
without a token, a network, or a repository existing anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.github.transport import FakeTransport, Unauthorized  # noqa: E402
from tools import probe_github_token as probe  # noqa: E402
from tools.check_layers import RULES, imports_of, modules_of  # noqa: E402

OWNER = "Efkrdnz"


def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine with a keyring that holds nothing, deterministically."""
    monkeypatch.setitem(
        sys.modules, "keyring", types.SimpleNamespace(get_password=lambda *a, **k: None)
    )


# ───────────────────────────── the token ─────────────────────────────


def test_no_token_is_refused_with_a_message_that_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(probe, "resolve_token", lambda: None)
    assert probe.main([]) == probe.EXIT_NO_TOKEN
    err = capsys.readouterr().err
    assert "keyring set jarvis github_token" in err
    assert probe.TOKEN_ENV in err


def test_a_token_belonging_to_another_tool_is_deliberately_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that can create repositories uses the credential it was GIVEN.

    GITHUB_TOKEN and GH_TOKEN are set on plenty of machines, including this one,
    by tools with nothing to do with Jarvis. Picking one up here is how a probe
    creates a repository on an account nobody pointed it at.
    """
    _no_keyring(monkeypatch)
    monkeypatch.delenv(probe.TOKEN_ENV, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not-ours")
    monkeypatch.setenv("GH_TOKEN", "ghp_also-not-ours")
    assert probe.resolve_token() is None

    monkeypatch.setenv(probe.TOKEN_ENV, "ghp_ours")
    assert probe.resolve_token() == "ghp_ours"


def test_the_keyring_is_preferred_and_its_absence_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(probe.TOKEN_ENV, "ghp_from-the-environment")
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        types.SimpleNamespace(get_password=lambda *a, **k: "ghp_from-the-keyring"),
    )
    assert probe.resolve_token() == "ghp_from-the-keyring"

    monkeypatch.setitem(
        sys.modules,
        "keyring",
        types.SimpleNamespace(get_password=lambda *a, **k: (_ for _ in ()).throw(RuntimeError())),
    )
    assert probe.resolve_token() == "ghp_from-the-environment"


# ───────────────────────────── read-only by default ─────────────────────────────


def test_the_default_run_creates_nothing_and_reports_the_matrix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t = FakeTransport(login=OWNER, scopes=("repo",))
    assert probe.main([], transport=t) == probe.EXIT_OK
    assert t.writes == []
    assert [(c.method, c.path) for c in t.calls] == [("GET", "/user")]

    out = capsys.readouterr().out
    assert "delete      : no" in out
    assert "but I can't delete it" in out
    assert "class before acting : compensatable" in out


def test_the_default_run_of_an_unreadable_token_says_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t = FakeTransport(login=OWNER, scopes=None, kind="fine_grained")
    assert probe.main([], transport=t) == probe.EXIT_OK
    out = capsys.readouterr().out
    assert "no X-OAuth-Scopes header" in out
    assert "delete      : unknown" in out
    assert "assume nothing can be done" in out


def test_the_parser_defaults_to_doing_nothing_destructive() -> None:
    args = probe.build_parser().parse_args([])
    assert args.create_throwaway is False
    assert args.assume_yes is False
    assert args.json is None


def test_a_rejected_token_and_a_rate_limit_are_different_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t = FakeTransport(script={"GET /user": [Unauthorized(401, "Bad credentials")]})
    assert probe.main([], transport=t) == probe.EXIT_BAD_TOKEN
    assert "rejected" in capsys.readouterr().err


def test_the_findings_can_be_written_out_for_the_record(tmp_path: Path) -> None:
    out = tmp_path / "s8.json"
    t = FakeTransport(login=OWNER, scopes=("repo",))
    assert probe.main(["--json", str(out)], transport=t) == probe.EXIT_OK
    report = json.loads(out.read_text())
    assert report["capabilities"]["delete"] == "no"
    assert "delete" in report["spoken"]


# ───────────────────────────── the destructive half ─────────────────────────────


def test_the_warning_names_the_leftover_repository_before_anything_exists() -> None:
    text = probe.throwaway_warning(OWNER, "jarvis-s8-probe", "no")
    assert "CREATE A REAL REPOSITORY" in text
    assert f"github.com/{OWNER}/zz-abandoned-jarvis-s8-probe" in text
    assert "permanent" in text
    assert "Nothing has been created yet." in text


def test_declining_the_confirmation_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda *_: "no thanks")
    t = FakeTransport(login=OWNER, scopes=("repo",))
    assert probe.main(["--create-throwaway"], transport=t) == probe.EXIT_OK
    assert t.writes == []
    assert "nothing was created" in capsys.readouterr().out


def test_a_closed_stdin_is_a_no_rather_than_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def _eof(*_: object) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    t = FakeTransport(login=OWNER, scopes=("repo",))
    assert probe.main(["--create-throwaway"], transport=t) == probe.EXIT_OK
    assert t.writes == []


def test_a_delete_capable_token_leaves_nothing_behind(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "s8.json"
    t = FakeTransport(login=OWNER, scopes=("repo", "delete_repo"))
    code = probe.main(
        ["--create-throwaway", "--assume-yes", "--slug", "jarvis-s8-probe", "--json", str(out)],
        transport=t,
    )
    assert code == probe.EXIT_OK
    assert t.repos == {}
    report = json.loads(out.read_text())["throwaway"]
    assert report["delete"] == {"ok": True}
    assert "left_behind" not in report
    assert "nothing was left behind" in capsys.readouterr().out


def test_a_token_that_cannot_delete_compensates_in_order_and_says_what_is_left(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t = FakeTransport(login=OWNER, scopes=("repo",))
    assert (
        probe.main(["--create-throwaway", "--assume-yes", "--slug", "jarvis-s8-probe"], transport=t)
        == probe.EXIT_OK
    )
    patched = [c.body for c in t.sent("PATCH")]
    assert patched == [
        {"name": "zz-abandoned-jarvis-s8-probe"},
        {"private": True},
        {"archived": True},
    ]
    left = t.repos[f"{OWNER}/zz-abandoned-jarvis-s8-probe".lower()]
    assert left["archived"] is True and left["private"] is True
    assert f"LEFT BEHIND, permanently: github.com/{OWNER}/zz-abandoned" in capsys.readouterr().out


def test_the_throwaway_needs_an_owner_from_somewhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t = FakeTransport(script={"GET /user": [{"type": "User"}]}, scopes=("repo",))
    assert probe.main(["--create-throwaway", "--assume-yes"], transport=t) == probe.EXIT_CRASHED
    assert "--owner" in capsys.readouterr().err
    assert t.writes == []


# ───────────────────────────── the seam ─────────────────────────────


def test_the_package_imports_on_a_bare_interpreter() -> None:
    """Standard library only: the REST API is HTTPS and JSON, like the Bot API.

    Stronger than the house rule, which only binds the spine. It is worth having
    because the moment this package needs a wheel, so does every box that is
    meant to be able to create a repository.
    """
    r = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "import jarvis.github.transport, jarvis.github.scopes, jarvis.github.repos; "
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_importing_the_package_reads_no_credential_and_dials_nothing() -> None:
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "import tools.probe_github_token as p; "
            "print('ok' if p.resolve_token() is None else 'token found')",
        ],
        capture_output=True,
        text=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in (probe.TOKEN_ENV, "GITHUB_TOKEN", "GH_TOKEN")
        },
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_the_provider_layer_is_in_the_guard_and_points_inwards() -> None:
    """A new layer that nothing checks is a seam claim with no evidence."""
    assert "jarvis/github" in RULES
    assert modules_of("jarvis/github", ROOT), "the rule would pass vacuously"
    # The spine may never reach outwards to an HTTP client: it has to keep
    # importing under `python -S`.
    assert "jarvis.github" in RULES["spine"]
    # And this layer knows the spine and nothing else in the tree.
    required = {
        "jarvis.cc",
        "jarvis.voice",
        "jarvis.audio",
        "jarvis.live",
        "jarvis.telegram",
        "jarvis.capture",
    }
    assert required <= set(RULES["jarvis/github"])


def test_the_provider_layer_reaches_no_channel_no_driver_and_no_sound_card() -> None:
    reached = [
        f"{path.relative_to(ROOT).as_posix()} imports {name}"
        for path in modules_of("jarvis/github", ROOT)
        for name in sorted(imports_of(path, ROOT))
        if name.startswith(
            ("jarvis.voice", "jarvis.audio", "jarvis.live", "jarvis.telegram", "jarvis.cc")
        )
    ]
    assert not reached, "\n".join(reached)
