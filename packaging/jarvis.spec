# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Jarvis.exe: one folder, no console, everything inside.

    pip install -e ".[cc,voice,live,tts,geo,wake,secrets,app,build]"
    python packaging/make_icon.py          # only if the drawing changed
    pyinstaller packaging/jarvis.spec --noconfirm
    dist\\Jarvis\\Jarvis.exe --selftest --report selftest.json

ONE EXE, EVERY PROCESS. ``packaging/jarvis_app.py`` calls
``jarvis.app.entry.main``, which runs the app, or — given ``-m <module>`` —
one of the child processes the app supervises. Frozen, ``sys.executable`` IS
Jarvis.exe, so every ``[sys.executable, "-m", ...]`` spawn in the tree starts
this same file again. There is nothing else to bundle and nothing else to find.

ONE FOLDER, NOT ONE FILE. A one-file exe unpacks itself to %TEMP% on every
start. Jarvis starts four processes, so that would be four unpacks of a
bundle that carries the Claude Code CLI, and antivirus scanners inspect every
one of them. One folder starts instantly and is replaced by unzipping over it.

WHAT STATIC ANALYSIS CANNOT SEE is everything below: data directories read
through ``Path(__file__)``, native libraries opened with ``dlopen`` by a
package that only knows a path (PortAudio, onnxruntime), the Claude Code CLI
(an executable the SDK finds next to its own source), keyring backends found
through entry points, and the ``jarvis.*`` modules imported inside functions so
the scheduler does not pay for loading the desk. ``Jarvis.exe --selftest``
checks each of these from inside the built app, and CI runs it.

WHAT MUST NEVER BE INSIDE: openWakeWord's pretrained models. They are
CC BY-NC-SA and are downloaded on the user's machine at run time (ADR 0012).
The build refuses to finish if one turns up among the collected files.

WHAT MUST BE INSIDE, AND IS NOT IN GIT: Silero VAD's model, the thing that
tells a breath from a word. It is MIT, so it ships, with its notice; it is a
2 MB binary, so it is never committed. The workflow fetches it into
``packaging/models/`` with ``python -m jarvis.audio.vadmodel download``,
verified against the same pin the app uses, and on Windows this spec refuses
to build without it: an exe that quietly fell back to the basic detector
would let every headset breath through as a turn.

Windows is the target; the same spec runs on Linux as a check that it
evaluates and that analysis succeeds, with the Windows-only pieces (PortAudio
ships inside sounddevice's Windows wheel; tzdata is a Windows-only dependency)
reported as absent rather than failing.
"""

import importlib.util
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - PyInstaller defines SPECPATH
WINDOWS = sys.platform == "win32"

#: Repo-relative directories of NON-Python files that jarvis reads at run time,
#: and the glob taken from each. Each lands at the same relative path inside
#: the bundle, which is where ``Path(__file__).parent / ...`` looks for it.
#: tests/test_packaging.py walks jarvis/ and fails if a directory with data in
#: it is missing here: a new one would build, start, and fail in front of the
#: user, because no unit test runs from inside the frozen app.
DATA_DIRS = (
    ("jarvis/migrations", "*.sql"),
    ("jarvis/window/static", "*"),
)

#: Third-party packages whose data files and native libraries the analyser
#: does not follow, and whether a Windows build without them is broken.
#:   claude_agent_sdk   _bundled/claude.exe: the Claude Code CLI itself
#:   onnxruntime        onnxruntime.dll and its providers: the wake word
#:   _sounddevice_data  portaudio-binaries/libportaudio64bit.dll: the microphone
#:   soxr               the resampler between the mic's rate and the models'
#:   tzdata             Windows' Python has no zone database: "10:00 in Istanbul"
#:   certifi            the CA bundle the HTTPS clients verify against
#:   google.genai       the Gemini client (Live and text)
#:   keyring            the Windows Credential Manager backend
#:   edge_tts           the British reader voice (optional: SAPI still speaks)
#:   PIL / pystray      the tray icon (optional: the HUD's Quit still works)
PACKAGES = (
    ("claude_agent_sdk", True),
    ("onnxruntime", True),
    ("_sounddevice_data", True),
    ("soxr", True),
    ("tzdata", True),
    ("certifi", True),
    ("google.genai", True),
    ("keyring", True),
    ("edge_tts", False),
    ("PIL", False),
    ("pystray", False),
)

#: Single-file modules (no package directory to collect from), named so the
#: analyser includes them even though jarvis imports them inside a function.
#: ``_miniaudio`` is the compiled half of miniaudio, the MP3 decoder that lets
#: the British voice play on a machine with no ffmpeg, which is every Windows.
MODULES = ("miniaudio", "_miniaudio", "sounddevice", "_sounddevice")

#: Never in the bundle. openwakeword (and scipy/sklearn, which it drags in)
#: because its models are non-commercial and jarvis runs them on onnxruntime
#: directly; test and notebook tooling because nothing at run time imports it;
#: tkinter and matplotlib because a stray optional import would add 30 MB.
EXCLUDES = (
    "openwakeword",
    "scipy",
    "sklearn",
    "tflite_runtime",
    "tkinter",
    "_tkinter",
    "matplotlib",
    "IPython",
    "pytest",
    "_pytest",
    "PyInstaller",
    "tests",
)

#: The openWakeWord model files (jarvis/audio/wake.py MODELS). ADR 0012.
NEVER_BUNDLE = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis")

#: Where the workflow puts the Silero model before this runs (gitignored), and
#: the MIT notice that travels with it. Both land in vadmodel.BUNDLE_DIR.
VAD_MODEL_DIR = "packaging/models"
VAD_NOTICE = "packaging/licenses/silero-vad.txt"

_NATIVE = ("**/*.dll", "**/*.dylib", "**/*.so", "**/*.so.*", "**/*.pyd")


def _note(message):
    print(f"jarvis.spec: {message}", file=sys.stderr)


def _present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _is_package(name):
    spec = importlib.util.find_spec(name)
    return spec is not None and spec.submodule_search_locations is not None


def _jarvis_datas():
    out = []
    for rel, pattern in DATA_DIRS:
        found = sorted((ROOT / rel).glob(pattern))
        if not found:
            raise SystemExit(f"jarvis.spec: nothing matches {rel}/{pattern}; the app needs it")
        out += [(str(f), rel) for f in found if f.is_file()]
    return out


def _third_party():
    datas, binaries, missing = [], [], []
    for name, required in PACKAGES:
        if not _present(name):
            missing.append((name, required))
            continue
        if _is_package(name):
            # Native libraries go in once, as binaries, so their own DLL
            # dependencies are followed; tests and fixtures are never read.
            datas += collect_data_files(name, excludes=[*_NATIVE, "**/tests/**"])
            binaries += collect_dynamic_libs(name)
    hard = [n for n, required in missing if required]
    if hard and WINDOWS:
        raise SystemExit(
            "jarvis.spec: missing " + ", ".join(hard) + ". Install the app's extras first:\n"
            '  pip install -e ".[cc,voice,live,tts,geo,wake,secrets,app,build]"'
        )
    for name, required in missing:
        why = "needed on Windows" if required else "optional"
        _note(f"{name} is not installed here ({why}); the build will not carry it")
    # keyring finds its backends through entry points, which live in its metadata.
    if _present("keyring"):
        datas += copy_metadata("keyring")
    return datas, binaries


def _hiddenimports():
    names = list(collect_submodules("jarvis"))
    names += [m for m in MODULES if _present(m)]
    names += collect_submodules("keyring.backends") if _present("keyring") else []
    if _present("edge_tts"):
        names.append("edge_tts")
    if WINDOWS:
        # pystray picks its backend with importlib at run time.
        names.append("pystray._win32")
        names += collect_submodules("tzdata")
    return sorted(set(names))


def _vad_model():
    """The Silero model and its notice as data entries, checked against the app's own pin.

    Imported from jarvis rather than repeated here, so the build cannot carry a
    file the app would refuse when it looks for it at BUNDLE_DIR.
    """
    from jarvis.audio import vadmodel

    src = ROOT / VAD_MODEL_DIR / vadmodel.MODEL
    if not vadmodel.verified(src):
        why = "is not there" if not src.exists() else "does not match its pinned SHA-256"
        if WINDOWS:
            raise SystemExit(
                f"jarvis.spec: {src} {why}. Fetch it first:\n"
                f"  python -m jarvis.audio.vadmodel download {VAD_MODEL_DIR}"
            )
        _note(f"{src} {why}; this build will not carry the voice activity model")
        return []
    return [(str(src), vadmodel.BUNDLE_DIR), (str(ROOT / VAD_NOTICE), vadmodel.BUNDLE_DIR)]


def _refuse_wake_models(entries):
    bad = sorted(
        dest
        for dest, src, _kind in entries
        if any(part in Path(src).name for part in NEVER_BUNDLE) or "openwakeword" in src.lower()
    )
    if bad:
        raise SystemExit(
            "jarvis.spec: refusing to bundle openWakeWord's CC BY-NC-SA models: "
            + ", ".join(bad)
            + "\nThey are downloaded on the user's machine; see docs/adr/0012."
        )


def _version_info():
    """File > Properties, and the name Task Manager shows for every Jarvis process."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    text = (ROOT / "jarvis" / "__init__.py").read_text(encoding="utf-8")
    version = re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)
    nums = (tuple(int(n) for n in re.findall(r"\d+", version))[:3] + (0, 0, 0, 0))[:4]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("FileDescription", "Jarvis"),
                            StringStruct("ProductName", "Jarvis"),
                            StringStruct("FileVersion", version),
                            StringStruct("ProductVersion", version),
                            StringStruct("OriginalFilename", "Jarvis.exe"),
                            StringStruct("LegalCopyright", "MIT licence"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


sys.path.insert(0, str(ROOT))
_datas, _binaries = _third_party()

a = Analysis(  # noqa: F821 - PyInstaller defines the build classes in the spec's namespace
    [str(ROOT / "packaging" / "jarvis_app.py")],
    pathex=[str(ROOT)],
    binaries=_binaries,
    datas=_jarvis_datas() + _datas + _vad_model(),
    hiddenimports=_hiddenimports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(EXCLUDES),
    noarchive=False,
    optimize=0,
)
_refuse_wake_models(a.datas)
_refuse_wake_models(a.binaries)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Jarvis",
    icon=str(ROOT / "packaging" / "jarvis.ico"),
    version=_version_info() if WINDOWS else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off: it buys a few MB and costs antivirus false positives on an
    # unsigned exe, and it has corrupted onnxruntime's and Qt-style DLLs before.
    upx=False,
    # No console, ever: the user double-clicks this. Every process writes its
    # output to a log under %LOCALAPPDATA%\Jarvis\logs instead.
    console=False,
    # Kept: if the app dies before its own logging starts (an import failure in
    # the bundle), the bootloader's dialog is the only thing the user will see.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Jarvis",
)
