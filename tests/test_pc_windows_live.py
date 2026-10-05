"""The real Windows APIs, on the windows-latest CI job. Nothing here changes the machine.

Each test reads something only a real Windows has — the Start menu, the shell's
known folders, the Core Audio endpoint, the window list — through exactly the
code the app runs, so a wrong argtype, a missing export, a bad GUID or a vtable
slot off by one fails here rather than in front of the user. The one call
that could act (AbortSystemShutdownW) is made only to confirm it reports
"nothing pending" and aborts nothing.

A CI runner may have no audio device and an empty desktop, so absence is
allowed where the user's machine would have the thing; a crash never is.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the real Windows APIs")

if sys.platform == "win32":
    from jarvis.pc.base import Volume
    from jarvis.pc.catalog import Match, launchable, match
    from jarvis.pc.windows import (
        ERROR_NOT_ALL_ASSIGNED,
        INPUT,
        KNOWN_FOLDERS,
        CoreAudio,
        Win32Api,
        WindowsDesktop,
    )


def test_every_signature_loads_against_the_real_dlls() -> None:
    api = Win32Api()  # a missing export or a bad argtype raises here
    assert api.advapi32.InitiateShutdownW.argtypes
    assert api.user32.SendInput.argtypes
    assert hasattr(os, "startfile")


def test_input_is_the_size_this_windows_expects() -> None:
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_the_start_menu_folders_come_from_the_shell() -> None:
    api = Win32Api()
    for key in ("programs", "common_programs"):
        assert Path(api.known_folder(KNOWN_FOLDERS[key])).is_dir(), key


def test_the_catalog_finds_notepad_or_settings_on_the_machine_itself() -> None:
    desk = WindowsDesktop()
    apps = desk.apps()
    # The Settings pages are always there; they are not evidence the scan worked.
    scanned = [a for a in apps if a.source != "settings"]
    assert scanned, desk.catalog.errors
    found = [match(n, scanned) for n in ("notepad", "settings", "not defteri", "ayarlar")]
    assert any(isinstance(f, Match) for f in found), sorted(a.name for a in scanned)[:60]
    assert all(launchable(a) for a in apps)


def test_get_startapps_runs_hidden_and_parses() -> None:
    apps = WindowsDesktop()._start_apps()
    assert isinstance(apps, list)
    assert all(a.target.startswith("shell:AppsFolder\\") for a in apps)


@pytest.mark.parametrize("place", ["desktop", "documents", "downloads", "home"])
def test_known_folders_resolve(place: str) -> None:
    path = WindowsDesktop().known_folder(place)
    assert path.is_absolute() and path.exists(), (place, path)


def test_the_volume_is_readable_or_honestly_unavailable() -> None:
    v = WindowsDesktop().volume()  # None on a runner with no output device; never a crash
    assert v is None or 0 <= v.level <= 100


def test_core_audio_opens_or_fails_with_an_hresult_not_a_crash() -> None:
    box: dict[str, object] = {}

    def probe() -> None:
        api = Win32Api()
        api.co_init()
        try:
            with CoreAudio(api.ole32, api.functype) as audio:
                box["got"] = audio.read()
        except OSError as exc:
            box["got"] = exc

    t = threading.Thread(target=probe)
    t.start()
    t.join(30)
    got = box.get("got")
    assert isinstance(got, Volume) or "HRESULT" in str(got), got


def test_the_open_windows_can_be_listed() -> None:
    for w in WindowsDesktop().windows():
        assert w.hwnd and w.title and w.pid >= 0


def test_abort_with_nothing_pending_aborts_nothing() -> None:
    try:
        assert Win32Api().abort_shutdown() is False
    except OSError as exc:
        if exc.errno == ERROR_NOT_ALL_ASSIGNED:
            pytest.skip("this account cannot enable SeShutdownPrivilege")
        raise
