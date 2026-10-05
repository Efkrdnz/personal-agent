"""The PC tools: who may call them, what needs a yes, and what the ledger keeps.

Driven through a FakeDesktop that records every call, so each test can say
both what Jarvis answered and what the computer was (or was not) asked to do.
The countdown tests fire the timer by hand and read the effect row through a
SECOND connection, because the process that holds the timer is not
necessarily the one that hears "cancel the shutdown".
"""

from __future__ import annotations

import ast
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis import effects as fx
from jarvis.db import connect, migrate
from jarvis.pc.base import App, PcStatus, Volume, Window
from jarvis.pc.catalog import settings_apps
from jarvis.tools.builtin import pc
from jarvis.tools.confirm import DIRECT_HUMAN, Confirmations, TypedTurns
from jarvis.tools.ctx import ToolCtx
from jarvis.tools.registry import Registry, ToolError
from jarvis.tools.reply import Reply

ROOT = Path(__file__).resolve().parents[1]


class FakeDesktop:
    backend = "fake"

    def __init__(
        self,
        *,
        apps: tuple[App, ...] = (),
        later: tuple[App, ...] = (),
        windows: tuple[Window, ...] = (),
        folders: dict[str, Path] | None = None,
        available: bool = True,
    ) -> None:
        self._apps = apps
        self._later = later
        self._windows = windows
        self.folders = folders or {}
        self.available = available
        self.calls: list[tuple[Any, ...]] = []
        self.timers: list[tuple[float, Callable[[], None]]] = []
        self.vol: Volume | None = Volume(50, False)
        self.still_open = False
        self.os_pending = False
        self.rescans = 0

    def status(self) -> PcStatus:
        return PcStatus(
            self.available, self.backend, "ok" if self.available else "No desktop here."
        )

    def warm(self) -> None:
        self.calls.append(("warm",))

    def apps(self) -> tuple[App, ...]:
        return self._apps

    def rescan(self) -> tuple[App, ...]:
        self.rescans += 1
        self._apps = self._apps + self._later
        return self._apps

    def launch(self, app: App) -> None:
        self.calls.append(("launch", app.target))

    def open_url(self, url: str) -> None:
        self.calls.append(("url", url))

    def open_path(self, path: Path) -> None:
        self.calls.append(("path", str(path)))

    def known_folder(self, place: str) -> Path:
        return self.folders[place]

    def windows(self) -> tuple[Window, ...]:
        return self._windows

    def close(self, window: Window) -> None:
        self.calls.append(("close", window.hwnd))

    def wait_closed(self, window: Window, timeout_s: float) -> bool:
        return not self.still_open

    def volume(self) -> Volume | None:
        return self.vol

    def set_volume(self, level: int) -> Volume | None:
        self.calls.append(("set_volume", level))
        self.vol = Volume(level, False)
        return self.vol

    def step_volume(self, delta: int) -> Volume | None:
        self.calls.append(("step_volume", delta))
        assert self.vol is not None
        self.vol = Volume(min(100, max(0, self.vol.level + delta)), False)
        return self.vol

    def set_mute(self, on: bool) -> Volume | None:
        self.calls.append(("set_mute", on))
        assert self.vol is not None
        self.vol = Volume(self.vol.level, on)
        return self.vol

    def media(self, action: str) -> None:
        self.calls.append(("media", action))

    def lock(self) -> None:
        self.calls.append(("lock",))

    def power(self, action: str) -> None:
        self.calls.append(("power", action))

    def sleep(self) -> None:
        self.calls.append(("sleep",))

    def cancel_power(self) -> bool:
        self.calls.append(("cancel_power",))
        return self.os_pending

    def after(self, delay_s: float, fn: Callable[[], None]) -> None:
        self.timers.append((delay_s, fn))

    def fire(self) -> None:
        timers, self.timers = self.timers, []
        for _, fn in timers:
            fn()


APPS = (
    App("Spotify", "shell:AppsFolder\\Spotify!App", "startapps"),
    App(
        "Hesap Makinesi",
        "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
        "startapps",
    ),
    App("Visual Studio Code", "shell:AppsFolder\\VSCode", "startapps"),
    App(
        "Visual Studio 2022",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\VS.lnk",
        "startmenu",
    ),
    *settings_apps(),
)
NOTEPAD = Window(11, "notes.txt - Notepad", 100, r"C:\Windows\notepad.exe", "Notepad")
NOTEPAD_2 = Window(12, "Untitled - Notepad", 101, r"C:\Windows\notepad.exe", "Notepad")
HUD = Window(13, "J.A.R.V.I.S.", 102, r"C:\Program Files\Microsoft\Edge\msedge.exe")
DESKTOP_WINDOW = Window(14, "Program Manager", 103, r"C:\Windows\explorer.exe", "Progman")


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "j.db")
    migrate(c)
    yield c
    c.close()


def talk(fake: FakeDesktop, channel: str = "desk") -> tuple[TypedTurns, dict[str, Any]]:
    """The shape of tests/test_tools_confirm.py's talk(): ``turns.said("yes")`` is the user."""
    turns = TypedTurns()
    extra = turns.keys(Confirmations(wait_s=0.0, sleep=lambda s: None))
    extra[pc.DESKTOP] = fake
    return turns, extra


def ctx(con: sqlite3.Connection, extra: dict[str, Any], channel: str = "desk") -> ToolCtx:
    return ToolCtx(con=con, channel=channel, actor=channel, extra=extra)


def events(con: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = con.execute("SELECT payload FROM events WHERE kind=? ORDER BY seq", (kind,))
    return [json.loads(r[0]) for r in rows]


def effect_rows(con: sqlite3.Connection, kind: str) -> list[fx.Effect]:
    return [e for e in fx.recent_effects(con, limit=50) if e.kind == kind]


# ───────────────────────────── the gate ─────────────────────────────


def _tool_calls() -> list[ast.Call]:
    tree = ast.parse((ROOT / "jarvis/tools/builtin/pc.py").read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Tool"
    ]


def test_every_pc_tool_names_its_channels_and_its_effect_explicitly() -> None:
    calls = _tool_calls()
    assert len(calls) == len(pc.TOOLS) == 9
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        # Written out, never inherited: the default is every channel, phone included.
        assert ast.literal_eval(kw["channels"]) == ("desk", "cli")
        assert isinstance(ast.literal_eval(kw["effect"]), str)
    for tool in pc.TOOLS:
        assert set(tool.channels) <= {"desk", "cli"}
        assert fx.reversibility_of(tool.effect or "")  # classified, or Registry.add refuses it
    Registry(pc.TOOLS)


@pytest.mark.parametrize("channel", ["telegram", "phone", "scheduler"])
def test_no_remote_channel_can_touch_the_computer(con: sqlite3.Connection, channel: str) -> None:
    fake = FakeDesktop(apps=APPS, windows=(NOTEPAD,))
    reg = Registry(pc.TOOLS)
    args = {
        "open_app": {"name": "spotify"},
        "open_website": {"url": "youtube.com"},
        "open_folder": {"place": "downloads"},
        "close_app": {"name": "notepad", "confirm": True},
        "volume": {"action": "mute"},
        "media": {"action": "next"},
        "lock_screen": {},
        "power": {"action": "shutdown", "confirm": True},
        "cancel_power": {},
    }
    _, extra = talk(fake)
    extra[DIRECT_HUMAN] = True  # not even a "click" from there
    for name in reg.names():
        said = reg.dispatch(name, args[name], ctx(con, extra, channel))
        assert said.startswith("I can't do that from here"), (name, said)
    assert reg.declarations(channel) == []
    assert fake.calls == [] and fake.timers == []
    assert effect_rows(con, "pc.power") == []


def test_no_desktop_is_a_sentence(con: sqlite3.Connection) -> None:
    with pytest.raises(ToolError, match="can't control this computer"):
        pc.open_app(ctx(con, {}), "spotify")
    with pytest.raises(ToolError, match="No desktop here."):
        pc.lock_screen(ctx(con, {pc.DESKTOP: FakeDesktop(available=False)}))


# ───────────────────────────── open ─────────────────────────────


def test_open_app_launches_the_catalog_entry(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(apps=APPS)
    assert pc.open_app(ctx(con, {pc.DESKTOP: fake}), "Spotify'ı") == "Opening Spotify."
    assert fake.calls == [("launch", "shell:AppsFolder\\Spotify!App")]


def test_open_app_reaches_a_settings_page(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(apps=APPS)
    said = pc.open_app(ctx(con, {pc.DESKTOP: fake}), "bluetooth ayarları")
    assert said == "Opening Bluetooth settings."
    assert fake.calls == [("launch", "ms-settings:bluetooth")]


def test_open_app_asks_which_when_two_fit(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(apps=APPS)
    said = pc.open_app(ctx(con, {pc.DESKTOP: fake}), "visual studio")
    assert "Visual Studio Code" in said and "Visual Studio 2022" in said and said.endswith("?")
    assert fake.calls == []


def test_a_miss_rescans_once_then_hands_the_model_the_names(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(apps=APPS)
    said = pc.open_app(ctx(con, {pc.DESKTOP: fake}), "Rechner")
    assert isinstance(said, Reply) and said.aloud is False
    assert said == "I couldn't find an app called Rechner on this computer."
    assert "Hesap Makinesi" in said.detail and "call open_app again" in said.detail
    assert "Bluetooth settings" not in said.detail  # pages are reachable by name, not listed
    assert fake.rescans == 1 and fake.calls == []


def test_an_app_installed_a_minute_ago_is_found_by_the_rescan(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(apps=APPS, later=(App("Discord", "shell:AppsFolder\\Discord", "startapps"),))
    assert pc.open_app(ctx(con, {pc.DESKTOP: fake}), "discord") == "Opening Discord."


@pytest.mark.parametrize(
    "name", [r"C:\Windows\System32\calc.exe", "cmd /c del *", "https://evil.example", "powershell"]
)
def test_what_the_model_says_is_never_launched(con: sqlite3.Connection, name: str) -> None:
    fake = FakeDesktop(apps=APPS)
    pc.open_app(ctx(con, {pc.DESKTOP: fake}), name)
    assert all(c[0] != "launch" or c[1] in {a.target for a in APPS} for c in fake.calls)
    assert not any(name in str(c) for c in fake.calls)


def test_open_website_opens_an_address_or_a_fixed_search(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    c = ctx(con, {pc.DESKTOP: fake})
    assert pc.open_website(c, url="www.youtube.com") == "Opening youtube.com."
    assert pc.open_website(c, query="lofi beats", site="youtube") == (
        "Searching YouTube for lofi beats."
    )
    assert fake.calls == [
        ("url", "https://www.youtube.com"),
        ("url", "https://www.youtube.com/results?search_query=lofi+beats"),
    ]
    for bad in ("javascript:alert(1)", "ms-msdt:/id x", "file:///C:/x.exe"):
        with pytest.raises(ToolError):
            pc.open_website(c, url=bad)
    with pytest.raises(ToolError, match="Which website"):
        pc.open_website(c)
    assert len(fake.calls) == 2


def test_open_folder_stays_inside_the_users_folder(con: sqlite3.Connection, tmp_path: Path) -> None:
    downloads = tmp_path / "İndirilenler"
    (downloads / "Invoices").mkdir(parents=True)
    (downloads / "report.pdf").write_text("pdf")
    (downloads / "setup.exe").write_text("MZ")
    fake = FakeDesktop(folders={"downloads": downloads})
    c = ctx(con, {pc.DESKTOP: fake})
    assert pc.open_folder(c, "downloads") == "Opening Downloads."
    assert pc.open_folder(c, "Downloads", subfolder="invoices") == "Opening Invoices in Downloads."
    assert pc.open_folder(c, "downloads", file="report.pdf") == "Opening report.pdf from Downloads."
    for sub, file in (("..", ""), ("", "setup.exe"), ("", "../../etc/passwd")):
        with pytest.raises(ToolError):
            pc.open_folder(c, "downloads", subfolder=sub, file=file)
    with pytest.raises(ToolError, match="desktop, documents"):
        pc.open_folder(c, "C:\\Windows")
    assert [Path(p).name for _, p in fake.calls] == ["İndirilenler", "Invoices", "report.pdf"]


# ───────────────────────────── close: a yes, and a record ─────────────────────────────


def test_close_reads_back_then_closes_once_on_the_users_yes(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD,))
    turns, extra = talk(fake)
    c = ctx(con, extra)
    turns.said("close notepad")
    first = pc.close_app(c, "notepad")
    assert first == (
        "I'll close the Notepad window showing notes.txt. If it has unsaved work, it will ask "
        "you first. Shall I go ahead? Say yes, or no."
    )
    assert fake.calls == []
    turns.said("yes")
    assert pc.close_app(c, "notepad", confirm=True) == "Closed Notepad."
    assert fake.calls == [("close", 11)]
    with pytest.raises(ToolError, match="haven't read that back"):
        pc.close_app(c, "notepad", confirm=True)  # one yes, one close
    assert fake.calls == [("close", 11)]

    (row,) = effect_rows(con, "pc.app_close")
    (granted,) = events(con, "confirm.granted")
    assert row.reversibility == "compensatable" and row.state == "applied"
    assert row.provider_ref is not None
    assert row.provider_ref["confirmed_by"] == f"desk:{granted['proposal']}"
    assert row.provider_ref["hwnd"] == 11 and row.provider_ref["exe"].endswith("notepad.exe")


def test_a_no_closes_nothing(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD,))
    turns, extra = talk(fake)
    c = ctx(con, extra)
    pc.close_app(c, "notepad")
    turns.said("no, wait")
    with pytest.raises(ToolError, match="said no"):
        pc.close_app(c, "notepad", confirm=True)
    assert fake.calls == [] and effect_rows(con, "pc.app_close") == []


def test_confirm_before_any_read_back_is_refused(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD,))
    turns, extra = talk(fake)
    turns.said("yes")
    with pytest.raises(ToolError, match="haven't read that back"):
        pc.close_app(ctx(con, extra), "notepad", confirm=True)
    assert fake.calls == []


def test_a_string_false_is_not_a_confirmation(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD,))
    _, extra = talk(fake)
    said = pc.close_app(ctx(con, extra), "notepad", confirm="false")  # type: ignore[arg-type]
    assert said.endswith("Shall I go ahead? Say yes, or no.") and fake.calls == []


def test_an_app_still_open_after_the_request_is_said_so(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD,))
    fake.still_open = True
    turns, extra = talk(fake)
    c = ctx(con, extra)
    pc.close_app(c, "notepad")
    turns.said("evet")
    said = pc.close_app(c, "notepad", confirm=True)
    assert "still open" in said and "save" in said


def test_two_windows_are_a_question_and_nothing_is_proposed(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD, NOTEPAD_2))
    _, extra = talk(fake)
    said = pc.close_app(ctx(con, extra), "notepad")
    assert '"notes.txt - Notepad"' in said and '"Untitled - Notepad"' in said
    assert said.endswith("Which one should I close?")
    assert events(con, "confirm.proposed") == []


@pytest.mark.parametrize(
    ("name", "why"), [("jarvis", "my own window"), ("program manager", "desktop")]
)
def test_jarvis_and_the_desktop_are_refused_before_any_read_back(
    con: sqlite3.Connection, name: str, why: str
) -> None:
    fake = FakeDesktop(windows=(NOTEPAD, HUD, DESKTOP_WINDOW))
    _, extra = talk(fake)
    with pytest.raises(ToolError, match=why):
        pc.close_app(ctx(con, extra), name)
    assert events(con, "confirm.proposed") == []


def test_no_such_window_names_what_is_open(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD, HUD))
    _, extra = talk(fake)
    with pytest.raises(ToolError, match="Open now: Notepad.") as e:
        pc.close_app(ctx(con, extra), "excel")
    assert "J.A.R.V.I.S." not in str(e.value)


def test_a_click_in_the_tools_tab_is_the_yes(con: sqlite3.Connection) -> None:
    fake = FakeDesktop(windows=(NOTEPAD,))
    extra = {pc.DESKTOP: fake, "confirmations": Confirmations(), DIRECT_HUMAN: True}
    assert pc.close_app(ctx(con, extra, "cli"), "notepad", confirm=True) == "Closed Notepad."
    (row,) = effect_rows(con, "pc.app_close")
    assert row.provider_ref is not None and row.provider_ref["confirmed_by"].startswith("cli:cf_")


def test_the_column_named_confirmed_by_cannot_hold_a_spoken_yes(con: sqlite3.Connection) -> None:
    # Why the proposal id lives in provider_ref: the column is a foreign key to
    # `requests`, and a spoken yes is deliberately not a request row. If this
    # starts passing, the schema changed and the id can move into the column.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        fx.record_effect(
            con,
            kind="pc.app_close",
            summary="I asked Notepad to close",
            reversibility="compensatable",
            confirmed_by="desk:cf_x",
        )


# ───────────────────────────── volume, media, lock ─────────────────────────────


def test_volume_up_down_and_set_say_the_level_read_back(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    c = ctx(con, {pc.DESKTOP: fake})
    assert pc.volume(c, "up") == "Volume is at 60 percent."
    assert pc.volume(c, "down", amount=25) == "Volume is at 35 percent."
    assert pc.volume(c, "set", level=80) == "Volume is at 80 percent."
    assert pc.volume(c, "up", amount=None) == "Volume is at 90 percent."  # null is the usual step
    assert fake.calls == [
        ("step_volume", 10),
        ("step_volume", -25),
        ("set_volume", 80),
        ("step_volume", 10),
    ]


def test_muting_warns_first_and_happens_after_the_warning(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    c = ctx(con, {pc.DESKTOP: fake})
    said = pc.volume(c, "mute")
    assert "silences me too" in said and "unmute" in said
    assert fake.calls == []  # not yet: the reader is still saying the warning
    assert [d for d, _ in fake.timers] == [pc.MUTE_DELAY_S]
    fake.fire()
    assert fake.calls == [("set_mute", True)]
    assert pc.volume(c, "unmute") == "Volume is at 50 percent."


@pytest.mark.parametrize(
    ("args"), [{"action": "set", "level": 0}, {"action": "down", "amount": 60}]
)
def test_silence_by_any_route_is_warned_about_too(
    con: sqlite3.Connection, args: dict[str, Any]
) -> None:
    fake = FakeDesktop()
    said = pc.volume(ctx(con, {pc.DESKTOP: fake}), **args)
    assert "silences me too" in said and fake.calls == []
    fake.fire()
    assert fake.calls == [("set_volume", 0)]


def test_a_level_that_cannot_be_read_back_is_said_as_about(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    fake.set_volume = lambda level: Volume(level, False, exact=False)  # type: ignore[method-assign]
    assert pc.volume(ctx(con, {pc.DESKTOP: fake}), "set", level=30) == (
        "Set the volume to about 30 percent."
    )


@pytest.mark.parametrize(
    "args", [{"action": "set"}, {"action": "set", "level": 150}, {"action": "louder"}]
)
def test_a_volume_request_that_makes_no_sense_is_refused(
    con: sqlite3.Connection, args: dict[str, Any]
) -> None:
    fake = FakeDesktop()
    with pytest.raises(ToolError):
        pc.volume(ctx(con, {pc.DESKTOP: fake}), **args)
    assert fake.calls == []


def test_media_and_lock(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    c = ctx(con, {pc.DESKTOP: fake})
    assert pc.media(c, "next") == "Skipping to the next track."
    assert pc.media(c, "pause") == "Pausing or resuming whatever is playing."
    assert pc.lock_screen(c) == "Locking the screen."
    assert fake.calls == [("media", "next"), ("media", "play_pause"), ("lock",)]
    with pytest.raises(ToolError):
        pc.media(c, "shuffle")


# ───────────────────────────── power: a yes, a countdown, a cancel ─────────────────────────────


def shut_down(con: sqlite3.Connection, fake: FakeDesktop, action: str = "shutdown") -> str:
    turns, extra = talk(fake)
    c = ctx(con, extra)
    turns.said(f"{action} the computer")
    first = pc.power(c, action)
    assert fake.timers == []
    turns.said("yes, go ahead")
    return first + " || " + pc.power(c, action, confirm=True)


def test_shutdown_is_read_back_then_counted_down(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    said = shut_down(con, fake)
    readback, done = said.split(" || ")
    assert "one-minute countdown" in readback and "unsaved work" in readback
    assert done.startswith("Shutting down in one minute.") and "cancel the shutdown" in done
    assert fake.calls == []  # nothing has happened yet: the minute is ours
    assert [d for d, _ in fake.timers] == [pc.POWER_GRACE_S]

    (row,) = effect_rows(con, "pc.power")
    (granted,) = events(con, "confirm.granted")
    assert row.undo_plan == {
        "op": pc.ABORT_OP,
        "args": {"action": "shutdown"},
        "speaks": "call it off",
    }
    assert row.provider_ref is not None
    assert row.provider_ref["confirmed_by"] == f"desk:{granted['proposal']}"
    left = fx.undo_window_remaining_s(row)
    assert left is not None and 55 < left <= 60

    fake.fire()
    assert fake.calls == [("power", "shutdown")]
    assert events(con, "pc.power")[-1]["outcome"] == "started"


def test_a_restart_counts_down_the_same_way(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    assert "Restarting in one minute." in shut_down(con, fake, "restart")
    fake.fire()
    assert fake.calls == [("power", "restart")]


def test_cancel_inside_the_minute_stops_it_from_any_process(
    con: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDesktop()
    monkeypatch.setattr(pc, "choose_desktop", lambda: fake)
    shut_down(con, fake)
    # "Cancel the shutdown" heard by another process: its own connection, same file.
    other = connect(tmp_path / "j.db")
    try:
        said = pc.cancel_power(ctx(other, {pc.DESKTOP: FakeDesktop()}, "cli"))
    finally:
        other.close()
    assert said == "Called off. The shutdown won't happen."
    assert ("cancel_power",) in fake.calls  # Windows' own pending shutdown, belt and braces
    (original,) = [e for e in effect_rows(con, "pc.power") if e.undo_plan]
    assert original.state == "undone"
    fake.fire()  # the minute ends in the process that started it
    assert ("power", "shutdown") not in fake.calls
    assert events(con, "pc.power")[-1]["outcome"] == "called_off"


def test_cancel_after_the_minute_is_honestly_too_late(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    shut_down(con, fake)
    con.execute(
        "UPDATE effects SET undo_deadline=? WHERE kind='pc.power'",
        (fx.deadline_in(-30),),
    )
    said = pc.cancel_power(ctx(con, {pc.DESKTOP: fake}))
    assert "too late" in said
    fake.fire()
    assert ("power", "shutdown") in fake.calls  # the user was told it would happen


def test_only_one_countdown_at_a_time(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    shut_down(con, fake)
    _, extra = talk(fake)
    with pytest.raises(ToolError, match="already counting down"):
        pc.power(ctx(con, extra), "sleep")


def test_cancel_with_nothing_of_ours_still_stops_one_windows_had(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    c = ctx(con, {pc.DESKTOP: fake})
    with pytest.raises(ToolError, match="no shutdown or restart counting down"):
        pc.cancel_power(c)
    fake.os_pending = True
    assert pc.cancel_power(c) == "I called off the shutdown Windows had pending."


def test_sleep_is_read_back_recorded_and_delayed(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    said = shut_down(con, fake, "sleep")
    assert "won't be able to hear you" in said and "Going to sleep in 5 seconds." in said
    (row,) = effect_rows(con, "pc.sleep")
    assert row.provider_ref is not None and row.provider_ref["confirmed_by"].startswith("desk:cf_")
    assert [d for d, _ in fake.timers] == [pc.SLEEP_DELAY_S] and fake.calls == []
    fake.fire()
    assert fake.calls == [("sleep",)]
    assert events(con, "pc.sleep")[-1]["outcome"] == "started"


def test_a_spoken_no_powers_nothing(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    turns, extra = talk(fake)
    c = ctx(con, extra)
    pc.power(c, "shut down")
    turns.said("hayır")
    with pytest.raises(ToolError, match="said no"):
        pc.power(c, "shut down", confirm=True)
    assert fake.timers == [] and effect_rows(con, "pc.power") == []


def test_no_file_to_check_means_no_countdown(tmp_path: Path) -> None:
    mem = connect(":memory:")
    migrate(mem)
    try:
        _, extra = talk(FakeDesktop())
        with pytest.raises(ToolError, match="countdown"):
            pc.power(ctx(mem, extra), "shutdown")
    finally:
        mem.close()


def test_power_through_the_registry_end_to_end(con: sqlite3.Connection) -> None:
    fake = FakeDesktop()
    turns, extra = talk(fake)
    reg = Registry(pc.TOOLS)
    c = ctx(con, extra)
    assert reg.dispatch("power", {"action": "restart"}, c).endswith("Say yes, or no.")
    turns.said("yes")
    assert reg.dispatch("power", {"action": "restart", "confirm": True}, c).startswith("Restarting")


def test_every_process_that_loads_the_tools_can_call_a_shutdown_off() -> None:
    assert pc.ABORT_OP in fx.registered_ops()
