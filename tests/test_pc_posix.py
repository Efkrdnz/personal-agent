"""Linux and macOS, best effort: the parsers, the argv, and a sentence for what is missing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from jarvis.pc.base import App, PcRefused, Volume
from jarvis.pc.posix import PosixDesktop, parse_desktop_entry, parse_pactl, parse_wpctl, user_dirs


class Runner:
    def __init__(self, out: dict[str, str] | None = None) -> None:
        self.out = out or {}
        self.argv: list[list[str]] = []
        self.spawned: list[list[str]] = []

    def run(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.argv.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.out.get(" ".join(argv[:2]), ""), "")

    def spawn(self, argv: list[str], **kw: Any) -> object:
        assert kw.get("start_new_session") is True  # detached: xdg-open may block
        self.spawned.append(argv)
        return object()


def linux(tmp_path: Path, runner: Runner, tools: set[str], **kw: Any) -> PosixDesktop:
    return PosixDesktop(
        platform="linux",
        env={"DISPLAY": ":0", "HOME": str(tmp_path)},
        which=lambda n: f"/usr/bin/{n}" if n in tools else None,
        run=runner.run,
        spawn=runner.spawn,
        home=tmp_path,
        **kw,
    )


def test_a_desktop_entry_is_an_app_unless_it_hides() -> None:
    assert parse_desktop_entry("[Desktop Entry]\nType=Application\nName=Firefox\n") == "Firefox"
    assert parse_desktop_entry("[Desktop Entry]\nName=X\nNoDisplay=true\n") is None
    assert parse_desktop_entry("[Desktop Entry]\nType=Link\nName=X\n") is None
    assert parse_desktop_entry("not an ini") is None


def test_volume_readers() -> None:
    assert parse_wpctl("Volume: 0.40 [MUTED]\n") == Volume(40, True)
    assert parse_wpctl("Volume: 1.00\n") == Volume(100, False)
    assert parse_pactl("Volume: front-left: 26214 /  40% / -23.88 dB", "Mute: no") == Volume(
        40, False
    )
    assert parse_wpctl("garbage") is None


def test_xdg_user_dirs_are_read(tmp_path: Path) -> None:
    text = 'XDG_DOWNLOAD_DIR="$HOME/İndirilenler"\nXDG_MUSIC_DIR="/data/music"\n'
    got = user_dirs(text, tmp_path)
    assert got == {"downloads": tmp_path / "İndirilenler", "music": Path("/data/music")}


def test_no_display_means_no_desktop(tmp_path: Path) -> None:
    d = PosixDesktop(platform="linux", env={"HOME": str(tmp_path)}, which=lambda n: None)
    assert not d.status().available and "no desktop session" in d.status().detail


def test_apps_come_from_desktop_files_and_launch_through_gtk_launch(tmp_path: Path) -> None:
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    (apps_dir / "firefox.desktop").write_text("[Desktop Entry]\nType=Application\nName=Firefox\n")
    (apps_dir / "hidden.desktop").write_text("[Desktop Entry]\nName=Hidden\nNoDisplay=true\n")
    r = Runner()
    d = linux(tmp_path, r, {"gtk-launch"}, data_dirs=[apps_dir])
    (firefox,) = d.apps()
    assert firefox == App("Firefox", "firefox.desktop", "desktop")
    d.launch(firefox)
    assert r.spawned == [["gtk-launch", "firefox"]]
    with pytest.raises(PcRefused):
        d.launch(App("Evil", "evil.desktop", "desktop"))


def test_a_missing_tool_is_named(tmp_path: Path) -> None:
    d = linux(tmp_path, Runner(), set())
    with pytest.raises(PcRefused, match="playerctl"):
        d.media("next")
    with pytest.raises(PcRefused, match="xdg-open"):
        d.open_url("https://example.com")


def test_volume_through_wpctl(tmp_path: Path) -> None:
    r = Runner({"wpctl get-volume": "Volume: 0.30\n"})
    d = linux(tmp_path, r, {"wpctl"})
    assert d.set_volume(30) == Volume(30, False)
    assert ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.30"] in r.argv


def test_linux_never_powers_off_by_killing_the_session(tmp_path: Path) -> None:
    r = Runner()
    with pytest.raises(PcRefused, match="without forcing"):
        linux(tmp_path, r, {"systemctl"}).power("shutdown")
    assert r.argv == [] and r.spawned == []
    linux(tmp_path, r, {"gnome-session-quit"}).power("restart")
    assert r.spawned == [["gnome-session-quit", "--reboot"]]


def test_known_folders_fall_back_to_the_usual_names(tmp_path: Path) -> None:
    d = linux(tmp_path, Runner(), set())
    assert d.known_folder("downloads") == tmp_path / "Downloads"
    assert d.known_folder("home") == tmp_path
