"""Which installed app the user meant, from a name heard in English or Turkish.

LAUNCH ONLY WHAT THE MACHINE LISTED. The catalog is built from the computer's
own lists — Start menu shortcuts, the App Paths registry, ``Get-StartApps`` —
plus a fixed allow-list of Settings pages. A spoken name only ever SELECTS an
entry; it is never handed to the shell, so "open calc; del *" and an injected
web page saying "open C:\\evil.exe" both select nothing.

THE MATCHING IS TURKISH-AWARE ON PURPOSE. Python's ``"İ".casefold()`` is ``i``
plus a combining dot and ``"ı"`` is not folded at all, so a plain casefold makes
"İnternet Seçenekleri" unreachable by "internet secenekleri". The suffix a
Turkish sentence puts on a name ("Spotify'ı", "Chrome'u") and the words around
it ("uygulamasını") are dropped before comparing.

A NAME IN ANOTHER LANGUAGE IS THE MODEL'S JOB, with help. "Calculator" against a
Turkish Windows' "Hesap Makinesi" shares no letters, so the built-in apps carry
both names here (:data:`ALIASES`); for anything else, a miss returns the nearest
installed names so the model can translate and ask again rather than guess.

PURE except :class:`Catalog`, which runs the loaders it is handed and caches what
they found: the PowerShell half takes seconds, and must not take them on every
request.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.pc.base import App, Window

__all__ = [
    "ALIASES",
    "AMBIGUOUS_MARGIN",
    "MATCH_THRESHOLD",
    "SETTINGS_PAGES",
    "SHORTCUT_SUFFIXES",
    "SOURCE_PRIORITY",
    "Ambiguous",
    "Catalog",
    "Match",
    "NotFound",
    "is_uninstaller",
    "launchable",
    "match",
    "match_window",
    "merge",
    "norm",
    "parse_start_apps",
    "rank",
    "scan_start_menu",
    "score",
    "settings_apps",
]

#: Below this nothing is launched. A whole-name fuzzy match reaches it only at a
#: SequenceMatcher ratio of 0.85: "discort" finds Discord, while "steam" (0.8
#: against "teams") does not open Microsoft Teams on a machine without Steam.
MATCH_THRESHOLD = 0.68
#: Two names this close are a question, not a choice: "visual studio" is Code or 2022.
AMBIGUOUS_MARGIN = 0.02
#: Each word a name has beyond the ones said costs this much, so "chrome" picks
#: Google Chrome over Chrome Remote Desktop instead of asking.
_EXTRA_WORD = 0.03

#: Where the same name appears twice, which source's entry is launched.
#: Get-StartApps is what the Start menu itself shows (localised names, Store
#: apps); a shortcut is next; App Paths is a bare executable name.
SOURCE_PRIORITY: Mapping[str, int] = {
    "startapps": 0,
    "startmenu": 1,
    "settings": 2,
    "apppaths": 3,
    "desktop": 1,
    "macapp": 1,
}

SHORTCUT_SUFFIXES = (".lnk", ".url", ".appref-ms")

_FOLD = str.maketrans({"ı": "i", "İ": "i", "’": "'", "‘": "'"})
#: The words around a name in a spoken request, in their folded form. "uygulama"
#: alone is NOT here: "Uygulama ayarları" is a Settings page.
_FILLER = frozenset(
    {
        "the", "a", "an", "my", "app", "application", "program", "programme", "please",
        "uygulamasi", "uygulamasini", "uygulamayi", "programi", "programini", "lutfen",
    }
)  # fmt: skip
_VERSION = re.compile(r"\(.*?\)|\bv?\d+(?:\.\d+)+\b|\b(?:x64|x86|64-bit|32-bit|64 bit|32 bit)\b")
_SUFFIX = re.compile(r"'\w+")
_INITIALISM = re.compile(r"\b(?:[^\W\d_]\.){2,}(?:[^\W\d_]\b)?")
_UNINSTALL = re.compile(r"\b(?:uninstall|uninstaller|kaldir|remove)\b")

#: Windows' own apps under both of their names, folded. Turkish Windows names
#: Calculator "Hesap Makinesi", and nothing about the letters says so.
ALIASES: tuple[tuple[str, ...], ...] = (
    ("calculator", "hesap makinesi"),
    ("notepad", "not defteri"),
    ("file explorer", "dosya gezgini", "explorer", "this pc", "bu bilgisayar"),
    ("settings", "ayarlar"),
    ("task manager", "gorev yoneticisi"),
    ("control panel", "denetim masasi"),
    ("command prompt", "komut istemi", "cmd"),
    ("snipping tool", "ekran alintisi araci"),
    ("camera", "kamera"),
    ("photos", "fotograflar"),
    ("clock", "saat", "alarms clock", "alarmlar saat"),
    ("calendar", "takvim"),
    ("mail", "posta"),
    ("weather", "hava durumu"),
    ("maps", "haritalar"),
    ("sticky notes", "yapiskan notlar"),
    ("sound recorder", "ses kaydedici"),
    ("media player", "medya oynaticisi"),
    ("terminal", "windows terminal"),
    ("microsoft store", "store", "magaza"),
    ("paint", "boya"),
)

#: The Settings pages "open bluetooth settings" may reach, and no others. A
#: model-made ``ms-settings:`` string is never opened: the URI scheme reaches
#: protocol handlers, which is a door this list keeps shut. The first name is
#: what Jarvis says; the rest are how it may be asked for, in either language.
SETTINGS_PAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ms-settings:", ("Settings", "Ayarlar", "Windows settings", "Windows ayarları")),
    ("ms-settings:display", ("Display settings", "Screen settings", "Ekran ayarları")),
    ("ms-settings:sound", ("Sound settings", "Audio settings", "Ses ayarları")),
    ("ms-settings:apps-volume", ("Volume mixer", "Ses karıştırıcı")),
    ("ms-settings:sound-devices", ("Sound devices", "Ses aygıtları")),
    ("ms-settings:bluetooth", ("Bluetooth settings", "Bluetooth", "Bluetooth ayarları")),
    ("ms-settings:network-wifi", ("Wi-Fi settings", "Wifi", "Wi-Fi ayarları", "Kablosuz ayarları")),
    (
        "ms-settings:network-status",
        ("Network settings", "Internet settings", "Ağ ayarları", "İnternet ayarları"),
    ),
    (
        "ms-settings:windowsupdate",
        ("Windows Update", "Update settings", "Güncelleme ayarları", "Windows güncelleme"),
    ),
    ("ms-settings:appsfeatures", ("Installed apps", "Apps settings", "Yüklü uygulamalar")),
    ("ms-settings:defaultapps", ("Default apps", "Varsayılan uygulamalar")),
    ("ms-settings:powersleep", ("Power settings", "Sleep settings", "Güç ayarları")),
    ("ms-settings:batterysaver", ("Battery settings", "Battery saver", "Pil ayarları")),
    ("ms-settings:nightlight", ("Night light", "Gece ışığı")),
    (
        "ms-settings:personalization-background",
        ("Background settings", "Wallpaper", "Desktop background", "Arka plan", "Duvar kağıdı"),
    ),
    ("ms-settings:themes", ("Themes", "Temalar")),
    ("ms-settings:mousetouchpad", ("Mouse settings", "Touchpad settings", "Fare ayarları")),
    ("ms-settings:typing", ("Keyboard settings", "Typing settings", "Klavye ayarları")),
    ("ms-settings:privacy-microphone", ("Microphone settings", "Mikrofon ayarları")),
    ("ms-settings:privacy-webcam", ("Camera settings", "Kamera ayarları")),
    ("ms-settings:notifications", ("Notification settings", "Bildirim ayarları")),
    ("ms-settings:quiethours", ("Do not disturb", "Focus assist", "Rahatsız etmeyin")),
    ("ms-settings:dateandtime", ("Date and time settings", "Tarih ve saat ayarları")),
    ("ms-settings:regionlanguage", ("Language settings", "Region settings", "Dil ayarları")),
    ("ms-settings:storagesense", ("Storage settings", "Depolama ayarları")),
    ("ms-settings:about", ("About this PC", "System information", "Sistem bilgisi")),
    ("ms-settings:printers", ("Printers", "Printer settings", "Yazıcılar")),
    ("ms-settings:clipboard", ("Clipboard settings", "Pano ayarları")),
)
_SETTINGS_URIS = frozenset(uri for uri, _ in SETTINGS_PAGES)


# ───────────────────────────── names ─────────────────────────────


def norm(text: str) -> str:
    """A name reduced to what two spellings of it have in common. Pure."""
    s = str(text or "").translate(_FOLD)
    # "J.A.R.V.I.S." is one word, said as one; split on its dots it is six letters.
    s = _INITIALISM.sub(lambda m: m.group(0).replace(".", ""), s)
    s = _SUFFIX.sub("", s)
    s = s.replace("++", " plus plus ").replace("+", " plus ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = _VERSION.sub(" ", s)
    words = [w for w in re.findall(r"[^\W_]+", s) if w not in _FILLER]
    return " ".join(words)


def _alias_index() -> dict[str, frozenset[str]]:
    index: dict[str, frozenset[str]] = {}
    for group in ALIASES:
        names = frozenset(norm(n) for n in group)
        for n in names:
            index[n] = names
    return index


def _spellings(query: str) -> list[tuple[str, float]]:
    """The query, and each other name it is known by with a hair less trust."""
    q = norm(query)
    if not q:
        return []
    out = [(q, 1.0)]
    out += [(a, 0.99) for a in sorted(_alias_index().get(q, frozenset()) - {q})]
    return out


def score(query: str, name: str) -> float:
    """How well a NORMALISED query names a NORMALISED name, 0..1. Pure."""
    if not query or not name:
        return 0.0
    if query == name:
        return 1.0
    if query.replace(" ", "") == name.replace(" ", ""):
        return 0.95  # "note pad", "you tube"
    qw, nw = set(query.split()), set(name.split())
    if qw <= nw:
        return 0.9 - _EXTRA_WORD * (len(nw) - len(qw))  # "chrome" -> "google chrome"
    if nw <= qw:
        return 0.85 - _EXTRA_WORD * (len(qw) - len(nw))  # "spotify music" -> "spotify"
    best = [
        max(difflib.SequenceMatcher(None, q, n).ratio() for n in name.split())
        for q in query.split()
    ]
    if all(b >= 0.85 for b in best):
        # Every word said is a near-spelling of a word in the name: "crome" -> Google Chrome.
        extra = max(0, len(nw) - len(qw))
        return 0.8 * (sum(best) / len(best)) - _EXTRA_WORD * extra
    return 0.8 * difflib.SequenceMatcher(None, query, name).ratio()


def rank(query: str, apps: Iterable[App], *, limit: int = 5) -> list[tuple[float, App]]:
    """Best first, one entry per name. Pure."""
    spellings = _spellings(query)
    if not spellings:
        return []
    scored: list[tuple[float, App]] = []
    for app in apps:
        n = norm(app.name)
        if not n:
            continue
        s = max(score(q, n) * trust for q, trust in spellings)
        scored.append((round(s, 4), app))
    scored.sort(key=lambda t: (-t[0], SOURCE_PRIORITY.get(t[1].source, 9), len(t[1].name)))
    names: set[str] = set()
    targets: set[str] = set()
    out: list[tuple[float, App]] = []
    for s, app in scored:
        # One target answers to several names (a Settings page and its Turkish
        # alias); one name can come from several sources. Either way it is one
        # thing to the user, and offering it twice is a question with one answer.
        name, target = norm(app.spoken), app.target.casefold()
        if name in names or target in targets:
            continue
        names.add(name)
        targets.add(target)
        out.append((s, app))
        if len(out) == limit:
            break
    return out


@dataclass(frozen=True, slots=True)
class Match:
    app: App
    score: float


@dataclass(frozen=True, slots=True)
class Ambiguous:
    apps: tuple[App, ...]


@dataclass(frozen=True, slots=True)
class NotFound:
    query: str
    near: tuple[App, ...]


def match(query: str, apps: Sequence[App]) -> Match | Ambiguous | NotFound:
    """One app, a question between up to three, or nothing with the nearest names. Pure."""
    ranked = rank(query, apps)
    if not ranked or ranked[0][0] < MATCH_THRESHOLD:
        near = tuple(a for s, a in ranked[:3] if s >= 0.35)
        return NotFound(query, near)
    top = ranked[0][0]
    tied = tuple(a for s, a in ranked if s >= MATCH_THRESHOLD and top - s <= AMBIGUOUS_MARGIN)
    if top < 1.0 and len(tied) > 1:
        return Ambiguous(tied[:3])
    return Match(ranked[0][1], top)


# ───────────────────────────── windows ─────────────────────────────


def _window_names(w: Window) -> list[str]:
    """What a user calls a window: its title, the app named at its end, its program."""
    names = [w.title]
    if " - " in w.title:
        names.append(w.title.rsplit(" - ", 1)[-1])  # "notes.txt - Notepad" -> "Notepad"
    if w.exe:
        names.append(Path(w.exe.replace("\\", "/")).stem)
    return names


def match_window(query: str, windows: Sequence[Window]) -> Match | Ambiguous | NotFound:
    """The same rules as :func:`match`, over open windows. ``App.target`` is the hwnd. Pure."""
    spellings = _spellings(query)
    if not spellings:
        return NotFound(query, ())
    scored: list[tuple[float, Window]] = []
    for w in windows:
        names = [norm(n) for n in _window_names(w)]
        s = max((score(q, n) * trust for q, trust in spellings for n in names if n), default=0.0)
        scored.append((round(s, 4), w))
    scored.sort(key=lambda t: -t[0])
    as_app = [(s, App(name=w.title, target=str(w.hwnd), source="window")) for s, w in scored]
    if not as_app or as_app[0][0] < MATCH_THRESHOLD:
        return NotFound(query, tuple(a for s, a in as_app[:3] if s >= 0.35))
    top = as_app[0][0]
    tied = tuple(a for s, a in as_app if top - s <= AMBIGUOUS_MARGIN)
    if len(tied) > 1:
        # Two windows of the same app are a real question even on an exact name:
        # "close Notepad" with two open must not pick one.
        return Ambiguous(tied[:3])
    return Match(as_app[0][1], top)


# ───────────────────────────── the sources ─────────────────────────────


def is_uninstaller(name: str, target: str = "") -> bool:
    """ "Uninstall Zoom" must never be what "close or open Zoom" finds. Pure."""
    if _UNINSTALL.search(norm(name)):
        return True
    base = Path(target.replace("\\", "/")).name.casefold()
    return base.startswith("unins") and base.endswith(".exe")


def scan_start_menu(
    roots: Iterable[Path | str],
    *,
    walk: Callable[[str], Iterable[tuple[str, list[str], list[str]]]] = os.walk,
) -> list[App]:
    """Every shortcut under the Start menu folders. ``walk`` is injected for tests."""
    out: list[App] = []
    for root in roots:
        for folder, _dirs, files in walk(str(root)):
            for f in files:
                stem, dot, ext = f.rpartition(".")
                if not dot or f".{ext.casefold()}" not in SHORTCUT_SUFFIXES or f == "desktop.ini":
                    continue
                path = os.path.join(folder, f)
                if is_uninstaller(stem, path):
                    continue
                out.append(App(name=stem, target=path, source="startmenu"))
    return out


def parse_start_apps(text: str) -> list[App]:
    """``Get-StartApps | ConvertTo-Json`` output. One app is an object, not a list."""
    body = (text or "").lstrip("\ufeff").strip()
    if not body:
        return []
    rows = json.loads(body)
    if isinstance(rows, dict):
        rows = [rows]
    out: list[App] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name, app_id = str(row.get("Name") or "").strip(), str(row.get("AppID") or "").strip()
        if not name or not app_id or any(c in app_id for c in "\r\n\t\x00"):
            continue
        target = "shell:AppsFolder\\" + app_id
        if is_uninstaller(name, app_id):
            continue
        out.append(App(name=name, target=target, source="startapps"))
    return out


def settings_apps() -> list[App]:
    """The Settings pages as catalog entries, each alias pointing at its page. Pure."""
    return [
        App(name=alias, target=uri, source="settings", display=names[0])
        for uri, names in SETTINGS_PAGES
        for alias in names
    ]


def merge(groups: Iterable[Iterable[App]]) -> tuple[App, ...]:
    """One entry per (name, target), the better source first. Pure."""
    best: dict[tuple[str, str], App] = {}
    for group in groups:
        for app in group:
            key = (norm(app.name), app.target.casefold())
            have = best.get(key)
            if have is None or SOURCE_PRIORITY.get(app.source, 9) < SOURCE_PRIORITY.get(
                have.source, 9
            ):
                best[key] = app
    return tuple(sorted(best.values(), key=lambda a: (a.name.casefold(), a.source)))


def launchable(app: App) -> bool:
    """Whether a target has the shape its source produces. Defence in depth, pure.

    The tools only ever hand a backend an App the catalog returned, so this is
    the second lock: a target that could only have come from the model (a URL,
    a command line, an ms-settings page nobody listed) is refused even if
    something upstream is wrong.
    """
    t = app.target
    if not t or any(c in t for c in '\r\n\x00"<>|'):
        return False
    if app.source == "settings":
        return t in _SETTINGS_URIS
    if app.source == "startapps":
        return t.startswith("shell:AppsFolder\\") and len(t) > len("shell:AppsFolder\\")
    if app.source == "startmenu":
        return t.casefold().endswith(SHORTCUT_SUFFIXES) and _absolute(t)
    if app.source == "apppaths":
        return t.casefold().endswith(".exe") and _absolute(t)
    if app.source == "macapp":
        return t.endswith(".app") and t.startswith("/")
    if app.source == "desktop":
        return t.endswith(".desktop") and "/" not in t
    return False


def _absolute(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("/")


# ───────────────────────────── the cache ─────────────────────────────


def _start_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="jarvis-pc-catalog", daemon=True).start()


@dataclass
class Catalog:
    """The installed apps, read once and kept; read again on a miss, but not twice a minute.

    ``sources`` maps a name to a loader. A loader that fails is recorded in
    :attr:`errors` and the others still count: a missing PowerShell must not
    make Notepad unopenable.
    """

    sources: Mapping[str, Callable[[], Iterable[App]]]
    clock: Callable[[], float] = time.monotonic
    spawn: Callable[[Callable[[], None]], None] = _start_thread
    #: A miss rescans only if the last read is older than this.
    min_rescan_s: float = 20.0
    #: How long a request waits for a read already under way.
    wait_s: float = 45.0
    errors: dict[str, str] = field(default_factory=dict)
    _apps: tuple[App, ...] | None = field(default=None, repr=False)
    _loaded_at: float = field(default=float("-inf"), repr=False)
    _pending: threading.Event | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def loaded(self) -> bool:
        return self._apps is not None

    def warm(self) -> None:
        """Start the first read in the background. Returns at once; idempotent."""
        event = self._claim(fresh=False)
        if event is not None:
            self.spawn(lambda: self._load(event))

    def apps(self) -> tuple[App, ...]:
        """The catalog, reading it now if nobody has yet."""
        return self._get(fresh=False)

    def rescan(self) -> tuple[App, ...]:
        """Read again after a miss, unless the last read was moments ago."""
        with self._lock:
            recent = self.clock() - self._loaded_at < self.min_rescan_s
        return self._get(fresh=not recent)

    def _get(self, *, fresh: bool) -> tuple[App, ...]:
        mine = self._claim(fresh=fresh)
        if mine is not None:
            self._load(mine)
        else:
            with self._lock:
                pending = self._pending
            if pending is not None:
                pending.wait(self.wait_s)
        with self._lock:
            return self._apps or ()

    def _claim(self, *, fresh: bool) -> threading.Event | None:
        """The event of a read THIS caller must do, or None when one is done or under way."""
        with self._lock:
            if self._pending is not None or (self._apps is not None and not fresh):
                return None
            self._pending = threading.Event()
            return self._pending

    def _load(self, event: threading.Event) -> None:
        groups: list[Iterable[App]] = []
        errors: dict[str, str] = {}
        for name, load in self.sources.items():
            try:
                groups.append(list(load()))
            except Exception as exc:  # noqa: BLE001 - one broken source must not empty the catalog
                errors[name] = f"{type(exc).__name__}: {exc}"[:300]
        merged = merge(groups)
        with self._lock:
            self._apps, self._loaded_at, self.errors = merged, self.clock(), errors
            self._pending = None
        event.set()
