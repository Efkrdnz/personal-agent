"""What a spoken request may open: web pages, fixed searches, files that run nothing.

The model's arguments can carry text from a web page or a document, so each
refusal here is a way an injected instruction could otherwise reach the shell:
a protocol handler, a program in Downloads, a folder link out of the user's
folders, the desktop's own window.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jarvis.pc.base import PcRefused, Window
from jarvis.pc.safety import (
    is_executable,
    protected_reason,
    resolve_inside,
    safe_url,
    search_url,
    site_name,
)

# ───────────────────────────── URLs ─────────────────────────────


@pytest.mark.parametrize(
    ("said", "opened"),
    [
        ("https://www.youtube.com/watch?v=x", "https://www.youtube.com/watch?v=x"),
        ("http://example.com", "http://example.com"),
        ("youtube.com", "https://youtube.com"),
        ("www.bbc.co.uk/news", "https://www.bbc.co.uk/news"),
        ("example.com:8080/x", "https://example.com:8080/x"),
        ("HTTPS://Example.com", "https://Example.com"),
        ("türkçe.com.tr", "https://türkçe.com.tr"),
    ],
)
def test_a_web_address_is_opened_as_http_or_https(said: str, opened: str) -> None:
    assert safe_url(said) == opened


@pytest.mark.parametrize(
    "said",
    [
        "",
        "javascript:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        "ms-msdt:/id PCWDiagnostic",
        "search-ms:query=x",
        "ms-settings:bluetooth",
        "data:text/html,<script>",
        "https://user@evil.example",
        "https://good.example\r\n.evil",
        "https://good.example/\u202eexe.pdf",
        "the weather in istanbul",
        "C:\\Windows\\System32\\calc.exe",
        "localhost:8080",
        "https://" + "a" * 2100 + ".com",
    ],
)
def test_anything_else_is_refused_with_a_sentence(said: str) -> None:
    with pytest.raises(PcRefused) as e:
        safe_url(said)
    assert str(e.value).endswith((".", "?"))


def test_the_site_is_said_without_www() -> None:
    assert site_name("https://www.youtube.com/watch?v=1") == "youtube.com"


@pytest.mark.parametrize(
    ("site", "start", "spoken"),
    [
        ("web", "https://www.google.com/search?q=", "the web"),
        ("youtube", "https://www.youtube.com/results?search_query=", "YouTube"),
        ("maps", "https://www.google.com/maps/search/?api=1&query=", "Maps"),
        ("wikipedia", "https://en.wikipedia.org/w/index.php?search=", "Wikipedia"),
    ],
)
def test_a_search_goes_through_a_fixed_template(site: str, start: str, spoken: str) -> None:
    url, said = search_url("lofi & chill?", site)
    assert url.startswith(start) and url.endswith("lofi+%26+chill%3F")
    assert said == spoken


def test_a_turkish_query_searches_turkish_wikipedia() -> None:
    url, _ = search_url("Atatürk Köşkü", "wikipedia")
    assert url.startswith("https://tr.wikipedia.org/")


def test_a_search_cannot_pick_its_own_site_or_be_empty() -> None:
    with pytest.raises(PcRefused):
        search_url("x", "evil.example")
    with pytest.raises(PcRefused):
        search_url("   ", "web")


# ───────────────────────────── files ─────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "setup.exe",
        "SETUP.EXE",
        "run.bat",
        "x.cmd",
        "x.ps1",
        "x.vbs",
        "x.js",
        "x.hta",
        "x.msi",
        "x.scr",
        "x.cpl",
        "x.reg",
        "x.jar",
        "Zoom.lnk",
        "site.url",
        "invoice.pdf.exe",
        "invoice.pdf.exe.",  # Windows strips the trailing dot and runs the .exe
        "invoice.pdf.exe  ",
    ],
)
def test_a_file_that_would_run_something_is_executable(name: str) -> None:
    assert is_executable(name, pathext="")


@pytest.mark.parametrize("name", ["report.pdf", "photo.JPG", "song.mp3", "notes.txt", "README"])
def test_a_document_is_not(name: str) -> None:
    assert not is_executable(name, pathext="")


def test_the_machines_own_pathext_counts_too() -> None:
    assert is_executable("tool.foo", pathext=".COM;.EXE;.FOO")


# ───────────────────────────── folders ─────────────────────────────


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    d = tmp_path / "Downloads"
    (d / "Invoices 2024").mkdir(parents=True)
    (d / "Invoices 2024" / "march.pdf").write_text("pdf")
    (d / "report.pdf").write_text("pdf")
    (d / "setup.exe").write_text("MZ")
    (tmp_path / "secret").mkdir()
    return d


def test_the_folder_itself(downloads: Path) -> None:
    assert resolve_inside(downloads) == downloads.resolve()


def test_a_subfolder_is_matched_without_case(downloads: Path) -> None:
    assert resolve_inside(downloads, "invoices 2024").name == "Invoices 2024"
    assert resolve_inside(downloads, "Invoices 2024", "MARCH.pdf").name == "march.pdf"


def test_a_missing_subfolder_names_what_is_there(downloads: Path) -> None:
    with pytest.raises(PcRefused, match="Invoices 2024"):
        resolve_inside(downloads, "invoices 2023")


@pytest.mark.parametrize(
    ("sub", "file"),
    [
        ("..", ""),
        ("../secret", ""),
        ("Invoices 2024/../..", ""),
        ("C:\\Windows", ""),
        ("/etc", ""),
        ("\\\\server\\share", ""),
        ("", "report.pdf:hidden.exe"),
        ("", "../secret"),
    ],
)
def test_nothing_outside_the_folder_is_reachable(downloads: Path, sub: str, file: str) -> None:
    with pytest.raises(PcRefused):
        resolve_inside(downloads, sub, file)


def test_a_program_in_the_folder_is_refused(downloads: Path) -> None:
    with pytest.raises(PcRefused, match="won't open programs"):
        resolve_inside(downloads, "", "setup.exe")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_link_out_of_the_folder_is_not_the_folder(downloads: Path) -> None:
    os.symlink(downloads.parent / "secret", downloads / "shortcut")
    with pytest.raises(PcRefused, match="outside"):
        resolve_inside(downloads, "shortcut")


# ───────────────────────────── windows ─────────────────────────────


@pytest.mark.parametrize(
    "w",
    [
        Window(1, "Program Manager", 10, r"C:\Windows\explorer.exe", cls="Progman"),
        Window(2, "", 10, r"C:\Windows\explorer.exe", cls="Shell_TrayWnd"),
        Window(3, "Desktop Window Manager", 11, r"C:\Windows\System32\dwm.exe"),
        Window(4, "J.A.R.V.I.S.", 12, r"C:\Program Files\Microsoft\Edge\msedge.exe"),
        Window(5, "Jarvis", 13, r"C:\Program Files\Jarvis\Jarvis.exe", own=True),
    ],
)
def test_the_desktop_the_system_and_jarvis_itself_are_never_closed(w: Window) -> None:
    assert protected_reason(w)


def test_an_ordinary_folder_window_may_be_closed() -> None:
    folder = Window(9, "Downloads", 10, r"C:\Windows\explorer.exe", cls="CabinetWClass")
    assert protected_reason(folder) is None
