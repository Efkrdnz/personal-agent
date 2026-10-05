"""Which browser opens the window, decided without a browser.

Every lookup is injected, so the Windows branch runs on Linux CI and the other
way round. The failures worth a test: Edge installed but not on PATH (the
normal Windows case) falling back to a tab; a browser that fails to start
ending the attempt instead of trying the next; and the URL, which carries the
token, ever passing through a shell.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

import pytest

from jarvis.window import launch
from jarvis.window.launch import app_argv, app_command, candidates, open_window

URL = "http://127.0.0.1:51234/#t=tok&en;$(rm -rf ~)|x"

LOCAL = r"C:\Users\me\AppData\Local"
PF86 = r"C:\Program Files (x86)"
PF = r"C:\Program Files"
WIN_ENV = {"LOCALAPPDATA": LOCAL, "ProgramFiles(x86)": PF86, "ProgramFiles": PF}

EDGE_LOCAL = LOCAL + r"\Microsoft\Edge\Application\msedge.exe"
EDGE_PF86 = PF86 + r"\Microsoft\Edge\Application\msedge.exe"
CHROME_PF = PF + r"\Google\Chrome\Application\chrome.exe"
CHROME_LOCAL = LOCAL + r"\Google\Chrome\Application\chrome.exe"

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAC_EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

SHELLS = {"sh", "/bin/sh", "bash", "/bin/bash", "cmd", "cmd.exe", "powershell", "pwsh"}


class Recorder:
    """A stand-in for Popen and webbrowser.open that remembers every call."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.fail = fail
        self.runs: list[tuple[list[str], dict[str, Any]]] = []
        self.opened: list[str] = []

    def run(self, argv: list[str], **kw: Any) -> object:
        self.runs.append((argv, kw))
        if argv[0] in self.fail:
            raise FileNotFoundError(argv[0])
        return object()

    def browser(self, url: str) -> bool:
        self.opened.append(url)
        return True


def nothing_on_path(name: str) -> str | None:
    return None


def only(*paths: str):
    return lambda p: p in paths


def _open(rec: Recorder, **kw: Any) -> str:
    kw.setdefault("which", nothing_on_path)
    kw.setdefault("exists", only())
    kw.setdefault("env", {})
    # These tests are about WHICH browser and HOW it is started; the redirect
    # that keeps the token off the command line has tests of its own below.
    kw.setdefault("redirect", lambda u: u)
    return open_window(URL, run=rec.run, browser=rec.browser, **kw)


# ───────────────────────────── Windows ─────────────────────────────


def test_windows_edge_from_localappdata_when_not_on_path() -> None:
    rec = Recorder()
    how = _open(rec, exists=only(EDGE_LOCAL), env=WIN_ENV, platform="win32")
    assert how == "edge-app"
    [(argv, kw)] = rec.runs
    assert argv == [EDGE_LOCAL, f"--app={URL}", "--window-size=1440,900"]
    assert rec.opened == []
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.DEVNULL
    assert kw["stderr"] is subprocess.DEVNULL
    assert kw["close_fds"] is True
    assert kw["creationflags"] == getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    assert "start_new_session" not in kw


def test_windows_prefers_the_machine_wide_edge() -> None:
    found = candidates(
        which=nothing_on_path,
        exists=only(EDGE_LOCAL, EDGE_PF86),
        env=WIN_ENV,
        platform="win32",
    )
    assert found == [("edge-app", EDGE_PF86), ("edge-app", EDGE_LOCAL)]


def test_windows_chrome_when_there_is_no_edge() -> None:
    rec = Recorder()
    how = _open(rec, exists=only(CHROME_LOCAL), env=WIN_ENV, platform="win32")
    assert how == "chrome-app"
    assert rec.runs[0][0][0] == CHROME_LOCAL


def test_windows_edge_beats_chrome() -> None:
    found = candidates(
        which=nothing_on_path,
        exists=only(CHROME_PF, EDGE_LOCAL),
        env=WIN_ENV,
        platform="win32",
    )
    assert [how for how, _ in found] == ["edge-app", "chrome-app"]


def test_windows_path_and_probe_naming_one_exe_try_it_once() -> None:
    def which(name: str) -> str | None:
        return EDGE_PF86.lower() if name == "msedge" else None

    found = candidates(which=which, exists=only(EDGE_PF86), env=WIN_ENV, platform="win32")
    assert found == [("edge-app", EDGE_PF86.lower())]


def test_windows_environment_names_are_case_insensitive() -> None:
    # dict(os.environ) on Windows has upper-cased keys.
    upper = {k.upper(): v for k, v in WIN_ENV.items()}
    found = candidates(which=nothing_on_path, exists=only(EDGE_LOCAL), env=upper, platform="win32")
    assert found == [("edge-app", EDGE_LOCAL)]


def test_windows_with_no_environment_does_not_probe_a_made_up_drive() -> None:
    seen: list[str] = []

    def exists(p: str) -> bool:
        seen.append(p)
        return True

    assert candidates(which=nothing_on_path, exists=exists, env={}, platform="win32") == []
    assert seen == []


# ───────────────────────────── falling through ─────────────────────────────


def test_a_browser_that_fails_to_start_falls_through_to_the_next() -> None:
    rec = Recorder(fail=(EDGE_LOCAL,))
    how = _open(rec, exists=only(EDGE_LOCAL, CHROME_PF), env=WIN_ENV, platform="win32")
    assert how == "chrome-app"
    assert [argv[0] for argv, _ in rec.runs] == [EDGE_LOCAL, CHROME_PF]
    assert rec.opened == []


def test_when_every_app_launch_fails_a_tab_still_opens() -> None:
    rec = Recorder(fail=(EDGE_LOCAL, CHROME_PF))
    how = _open(rec, exists=only(EDGE_LOCAL, CHROME_PF), env=WIN_ENV, platform="win32")
    assert how == "browser"
    assert rec.opened == [URL]


def test_nothing_installed_opens_the_default_browser() -> None:
    rec = Recorder()
    for platform in ("win32", "linux", "darwin"):
        assert _open(rec, env=WIN_ENV, platform=platform) == "browser"
    assert rec.runs == []
    assert rec.opened == [URL, URL, URL]
    assert app_command(URL, which=nothing_on_path, exists=only(), env={}, platform="linux") is None


def test_a_real_popen_of_a_missing_binary_falls_through(tmp_path) -> None:
    """The real Popen, with the real options, on this OS: they must be accepted."""
    missing = str(tmp_path / "no-such-browser")
    rec = Recorder()
    how = open_window(
        URL,
        which=lambda n: missing if n == "google-chrome" else None,
        exists=only(),
        browser=rec.browser,
        env={},
        platform=sys.platform,
        redirect=lambda u: u,
    )
    assert how == "browser"
    assert rec.opened == [URL]


# ───────────────────────────── never a shell ─────────────────────────────


def test_the_url_is_one_argv_element_and_no_shell_is_involved() -> None:
    rec = Recorder()
    for platform, kw in (
        ("win32", {"exists": only(EDGE_LOCAL), "env": WIN_ENV}),
        ("linux", {"which": lambda n: "/usr/bin/google-chrome" if n == "google-chrome" else None}),
        ("darwin", {"exists": only(MAC_CHROME)}),
    ):
        _open(rec, platform=platform, **kw)
    assert len(rec.runs) == 3
    for argv, kw in rec.runs:
        assert isinstance(argv, list)
        assert argv[0] not in SHELLS and os.path.basename(argv[0]) not in SHELLS
        assert argv[1] == f"--app={URL}"
        assert len(argv) == 3
        assert kw.get("shell", False) is False


def test_app_argv() -> None:
    assert app_argv("/x/chrome", "http://127.0.0.1:1/#t=a") == [
        "/x/chrome",
        "--app=http://127.0.0.1:1/#t=a",
        "--window-size=1440,900",
    ]


# ───────────────────────────── Linux and macOS ─────────────────────────────


def test_linux_finds_browsers_on_path_edge_first() -> None:
    on_path = {
        "microsoft-edge": "/usr/bin/microsoft-edge",
        "google-chrome": "/usr/bin/google-chrome",
        "chromium-browser": "/usr/bin/chromium-browser",
    }
    found = candidates(which=on_path.get, exists=only(), env={}, platform="linux")
    assert found == [
        ("edge-app", "/usr/bin/microsoft-edge"),
        ("chrome-app", "/usr/bin/google-chrome"),
        ("chrome-app", "/usr/bin/chromium-browser"),
    ]


def test_linux_chromium_alone_is_a_chrome_app() -> None:
    rec = Recorder()
    how = _open(rec, which=lambda n: "/snap/bin/chromium" if n == "chromium" else None)
    assert how == "chrome-app"
    argv, kw = rec.runs[0]
    assert argv[0] == "/snap/bin/chromium"
    # Its own session: a Ctrl+C in the window's terminal must not reach the browser.
    assert kw["start_new_session"] is True
    assert "creationflags" not in kw


def test_linux_never_probes_windows_or_mac_paths() -> None:
    seen: list[str] = []

    def exists(p: str) -> bool:
        seen.append(p)
        return True

    candidates(which=nothing_on_path, exists=exists, env=WIN_ENV, platform="linux")
    assert seen == []


def test_macos_app_bundles() -> None:
    rec = Recorder()
    assert _open(rec, exists=only(MAC_CHROME), platform="darwin") == "chrome-app"
    assert rec.runs[0][0][0] == MAC_CHROME
    found = candidates(
        which=nothing_on_path, exists=only(MAC_CHROME, MAC_EDGE), env={}, platform="darwin"
    )
    assert found == [("edge-app", MAC_EDGE), ("chrome-app", MAC_CHROME)]


def test_macos_user_applications_folder() -> None:
    mine = "/Users/me/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    found = candidates(
        which=nothing_on_path, exists=only(mine), env={"HOME": "/Users/me/"}, platform="darwin"
    )
    assert found == [("chrome-app", mine)]


def test_app_command_is_the_first_candidate() -> None:
    got = app_command(URL, which=nothing_on_path, exists=only(MAC_EDGE), env={}, platform="darwin")
    assert got == ("edge-app", [MAC_EDGE, f"--app={URL}", "--window-size=1440,900"])


def test_defaults_are_the_real_lookups() -> None:
    """The composition root calls ``open_window(url)`` bare; the defaults ARE the behaviour."""
    params = inspect.signature(open_window).parameters
    assert params["which"].default is shutil.which
    assert params["exists"].default is os.path.exists
    assert params["run"].default is subprocess.Popen
    assert params["browser"].default is webbrowser.open
    assert params["env"].default is os.environ
    assert params["platform"].default == sys.platform


@pytest.mark.parametrize("name", ["msedge", "chrome", "google-chrome", "chromium-browser"])
def test_every_documented_path_name_is_looked_up(name: str) -> None:
    asked: list[str] = []

    def which(n: str) -> str | None:
        asked.append(n)
        return None

    launch.candidates(which=which, exists=only(), env={}, platform="linux")
    assert name in asked


# ───────────────────────────── the token stays off the command line ─────────────────────────────


TOKEN = "tok_" + "s3cr3t" * 6


def test_the_token_never_reaches_a_browser_argv() -> None:
    """argv is world-readable via /proc on Linux; a review used it to answer a question."""
    url = f"http://127.0.0.1:5000/#t={TOKEN}"
    started: list[list[str]] = []
    opened: list[str] = []
    for which in (
        lambda n: "/usr/bin/google-chrome" if n == "google-chrome" else None,
        lambda n: None,
    ):
        how = open_window(
            url,
            which=which,
            exists=only(),
            env={},
            platform="linux",
            run=lambda argv, **kw: started.append(list(argv)),
            browser=opened.append,
        )
        assert how in ("chrome-app", "browser")
    assert started and opened
    for argv in started:
        assert not any(TOKEN in a for a in argv), argv
    assert not any(TOKEN in u for u in opened)
    assert all(a.startswith("--app=file://") for argv in started for a in argv[1:2])


def test_the_redirect_file_is_private_and_carries_the_url(tmp_path, monkeypatch) -> None:
    import stat
    import tempfile
    from urllib.parse import unquote, urlparse

    from jarvis.window.launch import redirect_file

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    url = f"http://127.0.0.1:5000/#t={TOKEN}"
    target = redirect_file(url, keep_s=60)
    path = Path(unquote(urlparse(target).path))
    assert target.startswith("file://") and TOKEN not in target
    body = path.read_text(encoding="utf-8")
    assert f"url={url}" in body and 'http-equiv="refresh"' in body
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_stale_redirect_files_are_swept(tmp_path, monkeypatch) -> None:
    import tempfile
    import time as time_

    from jarvis.window.launch import redirect_file

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = tmp_path / "jarvis-open-old"
    old.mkdir()
    (old / "open.html").write_text("x", encoding="utf-8")
    past = time_.time() - 3600
    os.utime(old, (past, past))
    redirect_file("http://127.0.0.1:1/#t=x", keep_s=60)
    assert not old.exists()


def test_the_redirect_escapes_what_it_writes(tmp_path, monkeypatch) -> None:
    import tempfile
    from urllib.parse import unquote, urlparse

    from jarvis.window.launch import redirect_file

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    target = redirect_file('http://127.0.0.1:1/#t="><script>x</script>', keep_s=60)
    body = Path(unquote(urlparse(target).path)).read_text(encoding="utf-8")
    assert "<script>" not in body
