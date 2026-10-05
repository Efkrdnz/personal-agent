"""``Jarvis.exe --selftest``: does THIS build have everything it needs, checked from inside it.

A frozen app fails in ways no unit test on the source tree can see: a native
library the bundler did not collect (PortAudio, onnxruntime), a data directory
left out (the migrations, the HUD's page), a keyring backend found by entry
point and therefore invisible to static analysis. Each of those starts fine and
fails later, in front of the user, with no console to say why. So CI runs the
built exe with this flag and reads the JSON it writes.

Every check runs on a thread with a time limit: a hung driver probe must fail
the check, not the CI job. A check that only means something on Windows is
reported as skipped elsewhere, never as passed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Check", "Skipped", "checks", "main", "passed", "run_checks"]

_DETAIL_CHARS = 500


class Skipped(Exception):
    """Not applicable on this platform. The message says why."""


@dataclass(frozen=True)
class Check:
    name: str
    #: Returns a one-line detail on success; raises on failure, Skipped when n/a.
    run: Callable[[], str]
    critical: bool = True


def run_checks(items: Sequence[Check], *, timeout_s: float = 60.0) -> dict[str, dict[str, Any]]:
    """Run every check, each on its own thread with a deadline. Never raises."""
    return {c.name: _run_one(c, timeout_s) for c in items}


def passed(results: dict[str, dict[str, Any]]) -> bool:
    """True when every CRITICAL check passed (or was skipped as not applicable)."""
    return all(r["ok"] for r in results.values() if r.get("critical", True))


def main(
    *,
    report: str | None = None,
    platform: str | None = None,
    items: Sequence[Check] | None = None,
    timeout_s: float = 60.0,
) -> int:
    """Run the checks, write the JSON report, return 0 only if every critical one passed."""
    platform = sys.platform if platform is None else platform
    results = run_checks(checks(platform) if items is None else items, timeout_s=timeout_s)
    ok = passed(results)
    body = json.dumps(results, indent=2, ensure_ascii=False)
    if report:
        Path(report).write_text(body + "\n", encoding="utf-8")
    print(body)
    failed = [n for n, r in results.items() if not r["ok"] and r.get("critical", True)]
    print("selftest passed" if ok else f"selftest FAILED: {', '.join(failed)}")
    return 0 if ok else 1


def _run_one(check: Check, timeout_s: float) -> dict[str, Any]:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["detail"] = str(check.run())
            box["ok"] = True
        except Skipped as why:
            box.update(ok=True, skipped=True, detail=f"skipped: {why}")
        except BaseException as exc:  # noqa: BLE001 - every failure is the finding
            box.update(ok=False, detail=f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=target, name=f"selftest-{check.name}", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive() or "ok" not in box:
        return {
            "ok": False,
            "detail": f"did not finish in {timeout_s:.0f}s",
            "critical": check.critical,
        }
    out: dict[str, Any] = {
        "ok": box["ok"],
        "detail": box["detail"][:_DETAIL_CHARS],
        "critical": check.critical,
    }
    if box.get("skipped"):
        out["skipped"] = True
    return out


# ───────────────────────────── the checks ─────────────────────────────


def checks(platform: str) -> tuple[Check, ...]:
    windows = platform.startswith("win")
    # PortAudio ships INSIDE the sounddevice wheel on Windows and macOS, so a
    # load failure there is a packaging bug. On Linux it is a system library
    # the app cannot carry, and its absence is the machine's, not the build's.
    bundled_portaudio = windows or platform == "darwin"
    return (
        Check("jarvis modules", _jarvis_modules),
        Check("numpy", _numpy),
        Check("portaudio", _portaudio, critical=bundled_portaudio),
        Check("soxr", _soxr),
        Check("onnxruntime", _onnxruntime),
        # Critical where the model ships inside the app: without it every
        # breath on a headset is a turn again, and nothing else would say so.
        Check("voice activity", _voice_activity, critical=windows),
        Check("google.genai", _genai),
        Check("keyring", lambda: _keyring(windows)),
        Check("claude code", _claude),
        Check("migrations", _migrations),
        Check("window files", _static),
        Check("database", _database),
        Check("window server", _window_server),
        Check("windows voice", lambda: _sapi(windows)),
        Check("time zones", _zones),
        Check("british voice", _edge, critical=False),
        Check("tray", _tray, critical=False),
    )


#: Imported lazily inside functions all over the tree, so a frozen build's
#: analyser can miss them; a child that cannot import its own entry module is
#: a process that exits before it can write a log line.
_MODULES = (
    "jarvis.__main__",
    "jarvis.schedule.__main__",
    "jarvis.telegram.__main__",
    "jarvis.cc.__main__",
    "jarvis.window.server",
    "jarvis.window.launch",
    "jarvis.tools.default",
    "jarvis.live.session",
    "jarvis.live.chat",
    "jarvis.live.persona",
    "jarvis.voice.engines",
    "jarvis.voice.desk",
    "jarvis.audio.devices",
    "jarvis.audio.wake",
    "jarvis.audio.vadmodel",
    "jarvis.audio.fillers",
    "jarvis.app.supervisor",
    "jarvis.app.setup",
    "jarvis.app.adapters",
    "jarvis.app.tray",
)


def _jarvis_modules() -> str:
    import importlib

    for name in _MODULES:
        importlib.import_module(name)
    return f"{len(_MODULES)} modules import"


def _numpy() -> str:
    import numpy

    return numpy.__version__


def _portaudio() -> str:
    try:
        import sounddevice as sd
    except OSError as exc:
        raise RuntimeError(f"PortAudio did not load: {exc}") from exc
    version = sd.get_portaudio_version()[1]
    try:
        n = len(sd.query_devices())
    except Exception as exc:  # noqa: BLE001 - a CI runner has no sound card; that is fine
        return f"{version} loaded; no devices ({exc})"
    return f"{version} loaded; {n} device(s)"


def _soxr() -> str:
    import numpy as np
    import soxr

    out = soxr.resample(np.zeros(480, dtype=np.float32), 48_000, 16_000)
    if len(out) != 160:
        raise RuntimeError(f"48 kHz -> 16 kHz gave {len(out)} samples, expected 160")
    return soxr.__version__


def _onnxruntime() -> str:
    import onnxruntime as ort

    return f"{ort.__version__} ({', '.join(ort.get_available_providers())})"


def _voice_activity(meipass: str | None = None) -> str:
    """The Silero model is where the build put it, and it can HEAR.

    Loading is not enough: a model fed its frames without the 64-sample context
    loads, runs and is deaf. So this scores a synthetic vowel and a breath with
    the same wrapper the desk uses. A frozen app must find its OWN copy, not
    one some earlier pip install left in the user's folder.
    """
    from jarvis.audio import vadmodel
    from jarvis.audio.dsp import SileroVad

    base = getattr(sys, "_MEIPASS", None) if meipass is None else meipass
    dirs = (Path(base) / vadmodel.BUNDLE_DIR,) if base else vadmodel.search_dirs()
    path = vadmodel.find(dirs)
    if path is None:
        if base:
            raise RuntimeError(
                f"{vadmodel.MODEL} is not inside the bundle ({', '.join(map(str, dirs))}); "
                "the build skipped packaging/models"
            )
        raise Skipped(f"no {vadmodel.MODEL} here yet; the desk downloads it on first use")
    heard = SileroVad(path, check=False).self_check()
    if not heard.ok:
        raise RuntimeError(f"{path} {heard.describe()}: the voice detector cannot tell them apart")
    return f"{heard.describe()}{_frozen_note(path)}"


def _genai() -> str:
    from google import genai

    return str(getattr(genai, "__version__", "imported"))


def _keyring(windows: bool) -> str:
    import keyring

    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    # Backends are found through entry points, which a bundler does not follow.
    # Without one, the HUD cannot store the Gemini key at all.
    if windows and "fail" in name.lower():
        raise RuntimeError(f"no usable keyring backend ({name})")
    return name


def _claude() -> str:
    import claude_agent_sdk  # noqa: F401 - the import is half the check
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    cli = SubprocessCLITransport.__new__(SubprocessCLITransport)._find_bundled_cli()
    if not cli:
        raise RuntimeError("claude_agent_sdk imports but its bundled CLI is missing")
    return str(cli)


def _migrations() -> str:
    from jarvis import db

    found = sorted(db.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not found:
        raise RuntimeError(f"no migrations in {db.MIGRATIONS_DIR}")
    return f"{len(found)} in {db.MIGRATIONS_DIR}{_frozen_note(db.MIGRATIONS_DIR)}"


def _static() -> str:
    from jarvis.window.server import STATIC_DIR

    missing = [n for n in ("index.html", "app.css", "app.js") if not (STATIC_DIR / n).is_file()]
    if missing:
        raise RuntimeError(f"missing from {STATIC_DIR}: {', '.join(missing)}")
    return f"{STATIC_DIR}{_frozen_note(STATIC_DIR)}"


def _frozen_note(path: Path) -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        return ""
    inside = Path(base) in path.resolve().parents
    return " (inside the bundle)" if inside else " (NOT inside the bundle)"


def _database() -> str:
    from jarvis import db

    newest = max(int(f.name[:3]) for f in db.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        con = db.connect(Path(d) / "selftest.db")
        try:
            version = db.migrate(con)
            con.execute("SELECT COUNT(*) FROM events").fetchone()
        finally:
            con.close()
    if version != newest:
        raise RuntimeError(f"migrated to {version}, expected {newest}")
    return f"a new database migrates to version {version}"


def _window_server() -> str:
    import urllib.request

    from jarvis import db
    from jarvis.tools.default import registry
    from jarvis.window.server import Services, make_server

    # No proxy: a corporate or sandbox HTTP proxy would otherwise be asked to
    # fetch 127.0.0.1, and its answer is not our server's.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = Path(d) / "window.db"
        db.open_db(path).close()
        services = Services(open_db=lambda: db.connect(path), registry=registry(), extra={})
        server = make_server(services, port=0)
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            codes = []
            for url, headers in (
                (base + "/", {}),
                (base + "/api/state", {"X-Jarvis-Token": server.token}),
            ):
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=20) as resp:
                    resp.read()
                    codes.append(resp.status)
        finally:
            server.shutdown()
    if codes != [200, 200]:
        raise RuntimeError(f"GET / and /api/state answered {codes}")
    return f"GET / and /api/state answered 200 on port {server.port}"


def _sapi(windows: bool) -> str:
    if not windows:
        raise Skipped("Windows only")
    from jarvis.voice.engines import SystemEngine

    pcm = SystemEngine().synth("Jarvis is online.", "en")
    if not pcm:
        raise RuntimeError("the Windows voice returned no audio")
    return f"{len(pcm)} bytes of speech"


def _zones() -> str:
    from zoneinfo import ZoneInfo

    ZoneInfo("Europe/Istanbul")
    return "Europe/Istanbul resolves"


def _edge() -> str:
    import edge_tts  # noqa: F401
    import miniaudio  # noqa: F401

    return "edge-tts and miniaudio import"


def _tray() -> str:
    from jarvis.app.tray import icon_image, load_pystray

    if load_pystray() is None:
        raise RuntimeError("pystray cannot run here; the app waits without a tray icon")
    icon_image(16)
    return "pystray imports and the icon draws"
