"""What a spoken request may make the computer open or close, decided by code.

Every function here is pure (or reads only the folder it was given) and every
refusal is a :class:`PcRefused` sentence, because the caller is a voice and a
traceback is not an answer.

WHY THE LISTS ARE SHORT AND FIXED. The model's arguments can carry text from
anywhere — a web page it searched, a document on the screen — so a URL is
opened only if it is http or https (``ms-msdt:`` and ``search-ms:`` reach
protocol handlers that have run code), a search goes through one of four fixed
templates, and a file is opened only if it is not something Windows would run.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

from jarvis.pc.base import PcRefused, Window

__all__ = [
    "EXECUTABLE_SUFFIXES",
    "PLACES",
    "PROTECTED_CLASSES",
    "PROTECTED_EXES",
    "SEARCH_SITES",
    "is_executable",
    "protected_reason",
    "resolve_inside",
    "safe_url",
    "search_url",
    "site_name",
]

#: The folders open_folder knows. Each backend maps them to a real path.
PLACES: tuple[str, ...] = (
    "desktop",
    "documents",
    "downloads",
    "pictures",
    "music",
    "videos",
    "home",
)

_MAX_URL = 2000
_MAX_QUERY = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")

#: site -> (template, the name Jarvis says). A query is percent-encoded into
#: the one ``{q}``; nothing else about the URL comes from the request.
SEARCH_SITES: dict[str, tuple[str, str]] = {
    "web": ("https://www.google.com/search?q={q}", "the web"),
    "youtube": ("https://www.youtube.com/results?search_query={q}", "YouTube"),
    "maps": ("https://www.google.com/maps/search/?api=1&query={q}", "Maps"),
    "wikipedia": ("https://{lang}.wikipedia.org/w/index.php?search={q}", "Wikipedia"),
}
_TURKISH_LETTERS = frozenset("çğıöşüÇĞİÖŞÜ")

#: Opened by double-click, these RUN something. PATHEXT adds the machine's own.
#: Shortcuts are here too: a .lnk or .url on the desktop can point at anything.
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".exe", ".com", ".bat", ".cmd", ".scr", ".pif", ".cpl", ".msc", ".msi", ".msp",
        ".mst", ".msix", ".msixbundle", ".appx", ".appxbundle", ".appinstaller",
        ".application", ".appref-ms", ".gadget", ".hta", ".inf", ".ins", ".isp", ".jar",
        ".js", ".jse", ".lnk", ".url", ".ps1", ".psm1", ".psd1", ".ps1xml", ".psc1", ".reg",
        ".scf", ".sct", ".shb", ".shs", ".vb", ".vbe", ".vbs", ".ws", ".wsc", ".wsf", ".wsh",
        ".settingcontent-ms", ".library-ms", ".search-ms", ".searchconnector-ms", ".diagcab",
        ".xll", ".py", ".pyw", ".pyz", ".sh", ".command", ".desktop", ".run", ".bin", ".app",
    }
)  # fmt: skip

#: The program behind the desktop, the taskbar and the sign-in screen. Asking
#: one of them to close is at best a no-op and at worst a frozen session.
PROTECTED_EXES: frozenset[str] = frozenset(
    {
        "dwm.exe", "winlogon.exe", "csrss.exe", "lsass.exe", "services.exe", "smss.exe",
        "wininit.exe", "svchost.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
        "logonui.exe", "lockapp.exe", "shellexperiencehost.exe",
        "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
        "textinputhost.exe", "runtimebroker.exe",
    }
)  # fmt: skip
#: File Explorer's folder windows may close; these explorer.exe windows may
#: not. WM_CLOSE to "Program Manager" (the desktop) opens the shut-down dialog.
PROTECTED_CLASSES: frozenset[str] = frozenset(
    {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
)
#: The HUD's page title (jarvis/window/static/index.html).
_HUD_TITLE = "J.A.R.V.I.S."


# ───────────────────────────── the web ─────────────────────────────


def safe_url(text: str) -> str:
    """An http(s) URL to open, or a refusal. A bare "youtube.com" gains https://."""
    raw = str(text or "").strip()
    if not raw:
        raise PcRefused("Which web address should I open?")
    if len(raw) > _MAX_URL or _CONTROL.search(raw) or re.search(r"\s", raw):
        raise PcRefused("That doesn't look like a web address I can open.")
    if "://" not in raw and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw):
        raw = "https://" + raw
    elif re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}:\d", raw):
        raw = "https://" + raw  # "example.com:8080/x" is a host and port, not a scheme
    parts = urlsplit(raw)
    if parts.scheme.casefold() not in ("http", "https"):
        raise PcRefused("I only open web pages — addresses starting http or https.")
    host = parts.hostname or ""
    try:
        # "türkçe.com" is a real address; its punycode form is what gets checked.
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise PcRefused("That doesn't look like a web address I can open.") from None
    if not host or "@" in parts.netloc or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
        raise PcRefused("That doesn't look like a web address I can open.")
    if "." not in host and host != "localhost" and ":" not in host:
        raise PcRefused("That doesn't look like a web address I can open.")
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc, parts.path, parts.query, parts.fragment)
    )


def site_name(url: str) -> str:
    """What to say for a URL: its host, without "www."."""
    host = urlsplit(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def search_url(query: str, site: str = "web") -> tuple[str, str]:
    """(url, the site's spoken name) for a search through a fixed template."""
    q = " ".join(str(query or "").split())
    if not q:
        raise PcRefused("What should I search for?")
    if len(q) > _MAX_QUERY or _CONTROL.search(q):
        raise PcRefused("That search is too long for me to open.")
    key = str(site or "web").strip().casefold() or "web"
    if key not in SEARCH_SITES:
        raise PcRefused("I can search the web, YouTube, Maps or Wikipedia.")
    template, spoken = SEARCH_SITES[key]
    lang = "tr" if any(c in _TURKISH_LETTERS for c in q) else "en"
    return template.format(q=quote_plus(q), lang=lang), spoken


# ───────────────────────────── files and folders ─────────────────────────────


def is_executable(name: str | Path, pathext: str | None = None) -> bool:
    """True when opening it would run something. Pure.

    Windows strips trailing dots and spaces when it opens a file, so
    "invoice.pdf.exe." is judged as the .exe it is.
    """
    base = Path(str(name).replace("\\", "/")).name.rstrip(". ")
    suffix = os.path.splitext(base)[1].casefold()
    if not suffix:
        return False
    extra = {
        e.strip().casefold()
        for e in (pathext if pathext is not None else os.environ.get("PATHEXT", "")).split(";")
        if e.strip()
    }
    return suffix in EXECUTABLE_SUFFIXES or suffix in extra


def _parts(text: str) -> list[str]:
    """Path components the user said, each checked to stay where it is."""
    raw = str(text or "").strip().strip("\"'")
    if not raw:
        return []
    if _CONTROL.search(raw) or raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        raise PcRefused("I only open folders inside your own folders.")
    parts = [p.strip() for p in re.split(r"[\\/]+", raw) if p.strip()]
    for p in parts:
        if p in (".", "..") or ":" in p or p.rstrip(". ") != p.rstrip():
            raise PcRefused("I only open folders inside your own folders.")
    return parts


def _child(parent: Path, name: str, *, want_dir: bool) -> Path:
    """``parent/name``, matched without case; a miss names what is there instead."""
    exact = parent / name
    if exact.exists() and exact.is_dir() == want_dir:
        return exact
    try:
        children = [c for c in parent.iterdir() if c.is_dir() == want_dir]
    except OSError:
        children = []
    folded = name.casefold()
    for c in children:
        if c.name.casefold() == folded:
            return c
    near = difflib.get_close_matches(name, [c.name for c in children], n=3, cutoff=0.5)
    kind = "folder" if want_dir else "file"
    hint = f" The nearest I can see: {_listed(near)}." if near else ""
    raise PcRefused(f"I can't find a {kind} called {name} in {parent.name or parent}.{hint}")


def resolve_inside(root: Path, subfolder: str = "", file: str = "") -> Path:
    """The folder or file the user named, provably inside ``root``, or a refusal.

    Every component is matched against what is really there, and the result is
    resolved (symlinks and junctions followed) and checked to still be under
    ``root`` — a link in Downloads that points at C:\\Windows is not Downloads.
    """
    base = Path(root).resolve()
    if not base.is_dir():
        raise PcRefused(f"I can't find your {Path(root).name or root} folder.")
    here = base
    for part in _parts(subfolder):
        here = _child(here, part, want_dir=True)
    file_parts = _parts(file)
    if file_parts:
        for part in file_parts[:-1]:
            here = _child(here, part, want_dir=True)
        if is_executable(file_parts[-1]):
            raise PcRefused("I won't open programs, scripts or shortcuts from a folder.")
        here = _child(here, file_parts[-1], want_dir=False)
    final = here.resolve()
    if final != base and base not in final.parents:
        raise PcRefused("That leads outside your folder, so I won't open it.")
    if final.is_file() and is_executable(final.name):
        raise PcRefused("I won't open programs, scripts or shortcuts from a folder.")
    return final


def _listed(items: Iterable[str]) -> str:
    names = list(items)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" and {names[-1]}"


# ───────────────────────────── windows ─────────────────────────────


def protected_reason(window: Window) -> str | None:
    """Why this window must not be closed, as a sentence; None when it may be. Pure."""
    if window.own or window.title.startswith(_HUD_TITLE):
        return "That's my own window. To close me, use Quit in the tray menu."
    if window.cls in PROTECTED_CLASSES:
        return "That's part of the Windows desktop itself, so I won't close it."
    exe = Path(window.exe.replace("\\", "/")).name.casefold()
    if exe in PROTECTED_EXES:
        return f"Windows needs {exe} to keep running, so I won't close it."
    return None
