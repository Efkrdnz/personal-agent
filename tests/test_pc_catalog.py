"""Which app the user meant: Turkish-aware, never an uninstaller, and a question when unsure.

The catalog is the line between "a sentence" and "something Windows runs", so
the tests pin the three ways it could go wrong: open the wrong thing (a tie
silently broken, an uninstaller matched, "steam" opening Teams), open nothing
when the right thing is there (a Turkish name, a suffix, a translation), or
read the whole Start menu again on every request.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from jarvis.pc import catalog as cat
from jarvis.pc.base import App, Window
from jarvis.pc.catalog import Ambiguous, Catalog, Match, NotFound, match, norm


def app(name: str, target: str = "", source: str = "startapps") -> App:
    return App(name, target or f"shell:AppsFolder\\{name.replace(' ', '')}!App", source)


CATALOG = (
    app("Google Chrome"),
    app("Chrome Remote Desktop"),
    app("Visual Studio Code"),
    app(
        "Visual Studio 2022",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\VS.lnk",
        "startmenu",
    ),
    app("Hesap Makinesi", "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    app("Not Defteri"),
    app(
        "Notepad++",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Notepad++.lnk",
        "startmenu",
    ),
    app("Spotify"),
    app("Microsoft Word"),
    app(
        "WordPad", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\WordPad.lnk", "startmenu"
    ),
    app("Dosya Gezgini"),
    app(
        "İnternet Seçenekleri",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\inet.lnk",
        "startmenu",
    ),
    app("Microsoft Teams"),
    app("Discord"),
    app("Microsoft Edge"),
    app(
        "Python 3.11 (64-bit)",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Py.lnk",
        "startmenu",
    ),
    *cat.settings_apps(),
)


def picked(said: str) -> str:
    found = match(said, CATALOG)
    assert isinstance(found, Match), f"{said!r} -> {found}"
    return found.app.spoken


# ───────────────────────────── names ─────────────────────────────


def test_turkish_dotted_and_dotless_i_fold_to_plain_i() -> None:
    # Python alone gets this wrong: "İ".casefold() is "i" plus a combining dot.
    assert "İ".casefold() != "i"
    assert norm("İNTERNET Seçenekleri") == "internet secenekleri"
    assert norm("ılık ışık") == "ilik isik"
    assert norm("Görev Yöneticisi") == "gorev yoneticisi"


def test_a_suffix_and_the_words_around_a_name_are_dropped() -> None:
    assert norm("Spotify'ı") == "spotify"
    assert norm("Chrome’u") == "chrome"
    assert norm("Spotify uygulamasını") == "spotify"
    assert norm("the Spotify app") == "spotify"


def test_an_initialism_is_one_word() -> None:
    # The HUD's title. Split on its dots, "a" is a filler word and "jarvis" never matches it.
    assert norm("J.A.R.V.I.S.") == "jarvis"
    assert norm("A.I. Studio") == "ai studio"


def test_versions_and_bitness_are_not_part_of_a_name() -> None:
    assert norm("Python 3.11 (64-bit)") == "python"
    assert norm("7-Zip x64") == "7 zip"
    assert norm("Notepad++") == "notepad plus plus"


@pytest.mark.parametrize(
    ("said", "want"),
    [
        ("chrome", "Google Chrome"),  # contained, and fewer extra words than Remote Desktop
        ("crome", "Google Chrome"),  # a misheard word
        ("Visual Studio Code", "Visual Studio Code"),
        ("hesap makinesi", "Hesap Makinesi"),
        ("spotify'ı", "Spotify"),
        ("Spotify uygulamasını", "Spotify"),
        ("internet secenekleri", "İnternet Seçenekleri"),
        ("discort", "Discord"),
        ("python", "Python 3.11 (64-bit)"),
        ("word", "Microsoft Word"),  # never WordPad
        ("notepad plus plus", "Notepad++"),
    ],
)
def test_a_spoken_name_finds_the_app(said: str, want: str) -> None:
    assert picked(said) == want


def test_an_english_name_finds_a_turkish_windows_app() -> None:
    # "Calculator" and "Hesap Makinesi" share no letters; the alias table is why this works.
    assert picked("calculator") == "Hesap Makinesi"
    assert picked("notepad") == "Not Defteri"
    assert picked("file explorer") == "Dosya Gezgini"


def test_two_equally_good_names_are_a_question() -> None:
    found = match("visual studio", CATALOG)
    assert isinstance(found, Ambiguous)
    assert {a.name for a in found.apps} == {"Visual Studio Code", "Visual Studio 2022"}


def test_a_near_spelling_of_a_different_app_is_not_opened() -> None:
    # "steam" is 0.8 like "teams": launching Teams for it would be a confident mistake.
    found = match("steam", CATALOG)
    assert isinstance(found, NotFound)


def test_nothing_found_names_what_is_nearest() -> None:
    found = match("spotiphy premiumz", (app("Spotify"), app("Discord")))
    assert isinstance(found, NotFound)
    assert [a.name for a in found.near] == ["Spotify"]
    missing = match("zzzz", CATALOG)
    assert isinstance(missing, NotFound) and missing.query == "zzzz" and missing.near == ()


def test_an_empty_name_matches_nothing() -> None:
    assert isinstance(match("", CATALOG), NotFound)
    assert isinstance(match("the app", CATALOG), NotFound)


# ───────────────────────────── settings ─────────────────────────────


@pytest.mark.parametrize(
    ("said", "uri"),
    [
        ("bluetooth settings", "ms-settings:bluetooth"),
        ("Bluetooth ayarları", "ms-settings:bluetooth"),
        ("volume mixer", "ms-settings:apps-volume"),
        ("wifi", "ms-settings:network-wifi"),
        ("windows update", "ms-settings:windowsupdate"),
        ("ayarlar", "ms-settings:"),
    ],
)
def test_a_settings_page_is_reached_by_name(said: str, uri: str) -> None:
    found = match(said, CATALOG)
    assert isinstance(found, Match) and found.app.target == uri


def test_a_turkish_alias_is_spoken_as_its_page() -> None:
    found = match("bluetooth ayarları", CATALOG)
    assert isinstance(found, Match) and found.app.spoken == "Bluetooth settings"


def test_every_settings_target_is_an_ms_settings_uri_from_the_list() -> None:
    for a in cat.settings_apps():
        assert a.target.startswith("ms-settings:") and cat.launchable(a), a
    assert not cat.launchable(App("x", "ms-settings:troubleshoot", "settings"))


# ───────────────────────────── the sources ─────────────────────────────


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("Uninstall Zoom", ""),
        ("Zoom Uninstaller", ""),
        ("Zoom'u Kaldır", ""),
        ("Zoom", r"C:\Program Files\Zoom\unins000.exe"),
    ],
)
def test_an_uninstaller_is_recognised(name: str, target: str) -> None:
    assert cat.is_uninstaller(name, target)


def test_the_start_menu_scan_keeps_shortcuts_and_drops_uninstallers(tmp_path: Path) -> None:
    user = tmp_path / "user"
    common = tmp_path / "common"
    (user / "Zoom").mkdir(parents=True)
    (common / "Accessories").mkdir(parents=True)
    (user / "Zoom" / "Zoom.lnk").write_text("")
    (user / "Zoom" / "Uninstall Zoom.lnk").write_text("")
    (user / "Zoom" / "desktop.ini").write_text("")
    (user / "Counter-Strike 2.url").write_text("")  # how Steam installs its games
    (common / "Accessories" / "Notepad.lnk").write_text("")
    (common / "Accessories" / "readme.txt").write_text("")
    apps = cat.scan_start_menu([user, common])
    assert sorted(a.name for a in apps) == ["Counter-Strike 2", "Notepad", "Zoom"]
    assert all(a.source == "startmenu" and Path(a.target).exists() for a in apps)


def test_get_startapps_output_is_parsed_with_its_bom_and_its_single_object_form() -> None:
    rows = [
        {"Name": "Hesap Makinesi", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
        {"Name": "Uninstall Foo", "AppID": "foo"},
        {"Name": "", "AppID": "nameless"},
        {"Name": "Bad", "AppID": "evil\r\nline"},
    ]
    apps = cat.parse_start_apps("\ufeff" + json.dumps(rows, ensure_ascii=False))
    assert [(a.name, a.target) for a in apps] == [
        ("Hesap Makinesi", "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    ]
    one = cat.parse_start_apps(json.dumps({"Name": "Spotify", "AppID": "Spotify!App"}))
    assert [a.name for a in one] == ["Spotify"]
    assert cat.parse_start_apps("") == [] and cat.parse_start_apps("\ufeff\r\n") == []


def test_the_same_app_from_two_sources_is_one_entry_from_the_better_source() -> None:
    merged = cat.merge(
        [
            [App("Notepad", r"C:\x\Notepad.lnk", "startmenu")],
            [App("Notepad", r"C:\x\Notepad.lnk", "apppaths")],
            [App("Notepad", "shell:AppsFolder\\notepad", "startapps")],
        ]
    )
    assert [a.source for a in merged] == ["startapps", "startmenu"]


@pytest.mark.parametrize(
    ("a", "ok"),
    [
        (App("Chrome", "shell:AppsFolder\\Chrome", "startapps"), True),
        (App("Chrome", "shell:AppsFolder\\", "startapps"), False),
        (App("Zoom", r"C:\Users\u\Zoom.lnk", "startmenu"), True),
        (App("Zoom", r"C:\Users\u\Zoom.exe", "startmenu"), False),
        (App("Zoom", r"Zoom.lnk", "startmenu"), False),  # relative: the cwd could plant one
        (App("chrome", r"C:\Program Files\Google\chrome.exe", "apppaths"), True),
        (App("chrome", "chrome.exe", "apppaths"), False),
        (App("x", "https://evil.example", "startapps"), False),
        (App("x", 'C:\\a.lnk" & calc', "startmenu"), False),
        (App("x", "anything", "model"), False),
    ],
)
def test_only_a_target_of_its_sources_shape_is_launchable(a: App, ok: bool) -> None:
    assert cat.launchable(a) is ok


# ───────────────────────────── the cache ─────────────────────────────


class Counting:
    def __init__(self, apps: list[App], fail: bool = False) -> None:
        self.apps, self.fail, self.calls = apps, fail, 0

    def __call__(self) -> list[App]:
        self.calls += 1
        if self.fail:
            raise OSError("powershell is missing")
        return self.apps


def test_the_catalog_is_read_once_and_kept() -> None:
    src = Counting([app("Spotify")])
    c = Catalog({"a": src})
    assert [a.name for a in c.apps()] == ["Spotify"]
    c.apps()
    c.apps()
    assert src.calls == 1


def test_a_miss_rescans_but_not_twice_in_a_row() -> None:
    now = [100.0]
    src = Counting([app("Spotify")])
    c = Catalog({"a": src}, clock=lambda: now[0], min_rescan_s=20.0)
    c.apps()
    c.rescan()  # moments later: the list is fresh enough
    assert src.calls == 1
    now[0] += 21
    c.rescan()
    assert src.calls == 2


def test_one_broken_source_does_not_empty_the_catalog() -> None:
    c = Catalog({"startapps": Counting([], fail=True), "startmenu": Counting([app("Notepad")])})
    assert [a.name for a in c.apps()] == ["Notepad"]
    assert "startapps" in c.errors and "powershell" in c.errors["startapps"]


def test_warming_reads_in_the_background_and_a_request_waits_for_it() -> None:
    started: list[threading.Thread] = []
    gate = threading.Event()

    def slow() -> list[App]:
        gate.wait(5)
        return [app("Spotify")]

    def spawn(fn):  # noqa: ANN001, ANN202
        t = threading.Thread(target=fn, daemon=True)
        started.append(t)
        t.start()

    calls = {"n": 0}

    def counted() -> list[App]:
        calls["n"] += 1
        return slow()

    c = Catalog({"a": counted}, spawn=spawn)
    c.warm()
    c.warm()  # idempotent while the first read is under way
    assert len(started) == 1
    gate.set()
    assert [a.name for a in c.apps()] == ["Spotify"]
    assert calls["n"] == 1


# ───────────────────────────── windows ─────────────────────────────


def win(hwnd: int, title: str, exe: str = "", pid: int = 0) -> Window:
    return Window(hwnd=hwnd, title=title, pid=pid or hwnd, exe=exe)


def test_a_window_is_found_by_the_app_named_at_the_end_of_its_title() -> None:
    windows = (
        win(1, "notes.txt - Notepad", r"C:\Windows\notepad.exe"),
        win(2, "YouTube - Google Chrome", r"C:\Program Files\Google\Chrome\chrome.exe"),
        win(3, "Document1 - Word", r"C:\Program Files\Microsoft Office\WINWORD.EXE"),
    )
    for said, hwnd in (("notepad", "1"), ("chrome", "2"), ("word", "3")):
        found = cat.match_window(said, windows)
        assert isinstance(found, Match) and found.app.target == hwnd, said


def test_two_windows_of_one_app_are_a_question_until_the_title_settles_it() -> None:
    windows = (
        win(1, "notes.txt - Notepad", r"C:\Windows\notepad.exe"),
        win(2, "Untitled - Notepad", r"C:\Windows\notepad.exe"),
    )
    assert isinstance(cat.match_window("notepad", windows), Ambiguous)
    found = cat.match_window("notes.txt - Notepad", windows)
    assert isinstance(found, Match) and found.app.target == "1"


def test_a_window_named_in_turkish_is_found_by_its_english_name() -> None:
    windows = (win(7, "Hesap Makinesi", r"C:\Windows\System32\ApplicationFrameHost.exe"),)
    found = cat.match_window("calculator", windows)
    assert isinstance(found, Match) and found.app.target == "7"
