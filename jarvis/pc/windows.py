"""Windows, through ctypes: the Start menu, the shell, user32, Core Audio and shutdown.

Written from Microsoft's documentation; what this Linux CI cannot run, the
windows-latest job runs non-destructively (tests/test_pc_windows_live.py).

FIXED-WIDTH TYPES IN EVERY STRUCT. ``ctypes.c_long`` is eight bytes on Linux and
four on Windows, and ``wintypes`` follows it, so a struct written with them has
one layout here and another there. With ``c_int32``/``c_uint32``/``c_size_t``
the Win64 layout can be asserted on the Linux CI: :class:`INPUT` MUST be 40
bytes, because ``SendInput`` silently does nothing when ``cbSize`` is wrong —
and an INPUT union that leaves out MOUSEINPUT (it is "only" for the keyboard)
is 32.

NO FORCE, EVER. ``shutdown /s /t 60`` implies ``/f``, and ``InitiateShutdownW``
with a grace period is documented to REQUIRE ``SHUTDOWN_FORCE_SELF``
(ERROR_INVALID_PARAMETER otherwise): a shutdown Windows counts down is a
shutdown that discards unsaved work. So the grace period is Jarvis's own (see
``jarvis/tools/builtin/pc.py``) and this module only ever asks for a shutdown
NOW, with no force flag — which Windows performs "interactively": every app is
asked, and one holding unsaved work stops it and shows the user why.

ONE COM THREAD. ShellExecute "should" run on a thread that called CoInitializeEx
as an apartment, and Core Audio needs COM on whatever thread calls it. The tools
run on asyncio's worker threads, which come and go, so every shell and audio
call is handed to one daemon thread that initialised COM once
(:class:`_Apartment`). A call that hangs there is abandoned after a timeout and
a fresh thread takes over; a daemon thread never holds up the app's exit.

Each instance loads its own ``WinDLL`` objects: setting ``argtypes`` on the
shared ``ctypes.windll`` would change them for every other caller in the process.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import itertools
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from jarvis.pc.base import App, PcRefused, PcStatus, Volume, Window
from jarvis.pc.catalog import Catalog, launchable, parse_start_apps, scan_start_menu, settings_apps
from jarvis.pc.safety import is_executable, safe_url

__all__ = [
    "CREATE_NO_WINDOW",
    "FORCE_FLAGS",
    "GUID",
    "INPUT",
    "KEYBDINPUT",
    "KNOWN_FOLDERS",
    "MEDIA_KEYS",
    "MOUSEINPUT",
    "SHUTDOWN_GRACE_S",
    "CoreAudio",
    "Win32Api",
    "WindowsDesktop",
]

#: CREATE_NO_WINDOW: a console program started from the windowed app would
#: otherwise get a console window of its own, flashing over the user's work.
CREATE_NO_WINDOW = 0x08000000

# ── winuser.h (virtual-key-codes, SendInput, WM_CLOSE, window styles) ──
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
MEDIA_KEYS: Mapping[str, int] = {
    "next": 0xB0,  # VK_MEDIA_NEXT_TRACK
    "previous": 0xB1,  # VK_MEDIA_PREV_TRACK
    "stop": 0xB2,  # VK_MEDIA_STOP
    "play_pause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
}
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
WM_CLOSE = 0x0010
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
#: DWMWINDOWATTRIBUTE's 14th member: a suspended UWP frame, or a window on
#: another virtual desktop, is "visible" and still not on the screen.
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_NOT_ALL_ASSIGNED = 1300
ERROR_SHUTDOWN_IN_PROGRESS = 1115
ERROR_NO_SHUTDOWN_IN_PROGRESS = 1116

# ── winreg.h: InitiateShutdownW ──
SHUTDOWN_FORCE_OTHERS = 0x1
SHUTDOWN_FORCE_SELF = 0x2
SHUTDOWN_RESTART = 0x4
SHUTDOWN_POWEROFF = 0x8
SHUTDOWN_GRACE_OVERRIDE = 0x20
#: Never passed. The test suite asserts no call carries any of them.
FORCE_FLAGS = SHUTDOWN_FORCE_OTHERS | SHUTDOWN_FORCE_SELF | SHUTDOWN_GRACE_OVERRIDE
#: Planned, "other": logged as something the user chose, not a crash.
SHTDN_REASON_FLAG_PLANNED = 0x80000000
#: Windows' own grace period. Zero, because any other value needs a force flag.
SHUTDOWN_GRACE_S = 0
TOKEN_ADJUST_PRIVILEGES = 0x20
TOKEN_QUERY = 0x8
SE_PRIVILEGE_ENABLED = 0x2

# ── objbase.h / Core Audio (mmdeviceapi.h, endpointvolume.h) ──
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
CLSCTX_INPROC_SERVER = 0x1
CLSCTX_ALL = 0x17
CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
E_RENDER, E_MULTIMEDIA = 0, 1
# Vtable slots, from the interface definitions (IUnknown is 0-2).
SLOT_RELEASE = 2
SLOT_GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator
SLOT_ACTIVATE = 3  # IMMDevice
SLOT_SET_MASTER_SCALAR = 7  # IAudioEndpointVolume
SLOT_GET_MASTER_SCALAR = 9
SLOT_SET_MUTE = 14
SLOT_GET_MUTE = 15

#: Known folders (knownfolderid.md). Asked of the shell rather than built from
#: %USERPROFILE%, because Windows 11 often moves Documents and Desktop to OneDrive.
KNOWN_FOLDERS: Mapping[str, str] = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "home": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "programs": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
    "common_programs": "{0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8}",
}
#: Where those two live by default, for when the shell will not say.
_PROGRAMS_FALLBACK = (
    ("programs", "APPDATA"),
    ("common_programs", "ProgramData"),
)
_START_MENU = ("Microsoft", "Windows", "Start Menu", "Programs")
_FOLDER_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}

_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
_STARTAPPS_OUT = "JARVIS_STARTAPPS_OUT"
#: Get-StartApps lists what the Start menu shows, Store apps included, under
#: their localised names. Written to a FILE: Windows PowerShell 5.1 writes a
#: redirected stdout in the console code page, which would mangle ş, ğ and ı.
#: The path arrives in an environment variable so no quoting can go wrong.
_STARTAPPS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress | "
    f"Out-File -Encoding utf8 -LiteralPath $env:{_STARTAPPS_OUT}"
)
STARTAPPS_TIMEOUT_S = 30.0


# ───────────────────────────── structs ─────────────────────────────

ULONG_PTR = ctypes.c_size_t  # pointer-sized on Win32 and Win64 alike
HRESULT = ctypes.c_int32


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        # bytes_le IS the in-memory layout: Data1-3 little-endian, Data4 as written.
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):  # noqa: N801 - the SDK's name
    # MOUSEINPUT is the largest member and sets the size even for a keyboard-only
    # caller: leave it out and every SendInput call fails without an error.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUT_UNION)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):  # noqa: N801 - the SDK's name
    _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_uint32)]


class TOKEN_PRIVILEGES(ctypes.Structure):  # noqa: N801 - the SDK's name
    _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


# ───────────────────────────── the DLLs ─────────────────────────────


def _load_dlls() -> dict[str, Any]:
    """Fresh WinDLL objects with argtypes set. Windows only."""
    from ctypes import wintypes as w

    dll = ctypes.WinDLL  # type: ignore[attr-defined]
    wndenumproc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)  # type: ignore[attr-defined]

    def sig(fn: Any, restype: Any, *argtypes: Any) -> None:
        fn.restype = restype
        fn.argtypes = list(argtypes)

    u = dll("user32", use_last_error=True)
    sig(u.EnumWindows, w.BOOL, wndenumproc, w.LPARAM)
    sig(u.IsWindowVisible, w.BOOL, w.HWND)
    sig(u.IsWindow, w.BOOL, w.HWND)
    sig(u.GetWindow, w.HWND, w.HWND, w.UINT)
    long_ptr = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW  # 32-bit has no ...Ptr
    sig(long_ptr, ctypes.c_ssize_t, w.HWND, ctypes.c_int)
    sig(u.GetWindowTextLengthW, ctypes.c_int, w.HWND)
    sig(u.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    sig(u.GetClassNameW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    sig(u.GetWindowThreadProcessId, w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
    sig(u.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(u.SendInput, w.UINT, w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    sig(u.LockWorkStation, w.BOOL)

    sh = dll("shell32", use_last_error=True)
    sig(
        sh.SHGetKnownFolderPath,
        ctypes.c_long,
        ctypes.POINTER(GUID),
        w.DWORD,
        w.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    ole = dll("ole32", use_last_error=True)
    sig(ole.CoInitializeEx, ctypes.c_long, ctypes.c_void_p, w.DWORD)
    sig(ole.CoTaskMemFree, None, ctypes.c_void_p)
    sig(
        ole.CoCreateInstance,
        ctypes.c_long,
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    k = dll("kernel32", use_last_error=True)
    sig(k.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    sig(k.QueryFullProcessImageNameW, w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
    sig(k.CloseHandle, w.BOOL, w.HANDLE)
    # Without a HANDLE restype the -1 pseudo-handle would arrive truncated to 32 bits.
    sig(k.GetCurrentProcess, w.HANDLE)
    adv = dll("advapi32", use_last_error=True)
    sig(adv.OpenProcessToken, w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    sig(adv.LookupPrivilegeValueW, w.BOOL, w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID))
    sig(
        adv.AdjustTokenPrivileges,
        w.BOOL,
        w.HANDLE,
        w.BOOL,
        ctypes.POINTER(TOKEN_PRIVILEGES),
        w.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    sig(adv.InitiateShutdownW, w.DWORD, w.LPWSTR, w.LPWSTR, w.DWORD, w.DWORD, w.DWORD)
    sig(adv.AbortSystemShutdownW, w.BOOL, w.LPWSTR)
    dwm = dll("dwmapi")
    sig(dwm.DwmGetWindowAttribute, ctypes.c_long, w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD)
    pw = dll("powrprof", use_last_error=True)
    sig(pw.SetSuspendState, w.BOOLEAN, w.BOOLEAN, w.BOOLEAN, w.BOOLEAN)
    return {
        "user32": u,
        "shell32": sh,
        "ole32": ole,
        "kernel32": k,
        "advapi32": adv,
        "dwmapi": dwm,
        "powrprof": pw,
        "enum_proc": wndenumproc,
        "functype": ctypes.WINFUNCTYPE,  # type: ignore[attr-defined]
        "last_error": ctypes.get_last_error,  # type: ignore[attr-defined]
        "get_window_long": long_ptr,
    }


class Win32Api:
    """Thin, typed wrappers over the calls Jarvis makes. Every DLL is injectable.

    Errors come back as ``OSError`` (``PermissionError`` for a window an elevated
    process owns); :class:`WindowsDesktop` turns them into sentences.
    """

    def __init__(self, **dlls: Any) -> None:
        missing = {
            "user32",
            "shell32",
            "ole32",
            "kernel32",
            "advapi32",
            "dwmapi",
            "powrprof",
            "enum_proc",
            "functype",
            "last_error",
        } - set(dlls)
        loaded = _load_dlls() if missing else {}
        self._d = {**loaded, **dlls}
        self.user32 = self._d["user32"]
        self.shell32 = self._d["shell32"]
        self.ole32 = self._d["ole32"]
        self.kernel32 = self._d["kernel32"]
        self.advapi32 = self._d["advapi32"]
        self.dwmapi = self._d["dwmapi"]
        self.powrprof = self._d["powrprof"]
        self.enum_proc = self._d["enum_proc"]
        self.functype = self._d["functype"]
        self.last_error: Callable[[], int] = self._d["last_error"]
        self._window_long = self._d.get("get_window_long") or getattr(
            self.user32, "GetWindowLongPtrW", None
        )

    # ── COM ──

    def co_init(self) -> None:
        # S_FALSE (already initialised) and RPC_E_CHANGED_MODE (already MTA) both
        # leave COM usable on this thread; a real failure shows up as the next
        # COM call's own error, which is where it can be put into words.
        self.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)

    def known_folder(self, guid: str) -> str:
        out = ctypes.c_wchar_p()
        hr = self.shell32.SHGetKnownFolderPath(
            ctypes.byref(GUID.parse(guid)), 0, None, ctypes.byref(out)
        )
        try:
            if hr != 0 or not out.value:
                raise OSError(f"SHGetKnownFolderPath failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
            return str(out.value)
        finally:
            # "whether SHGetKnownFolderPath succeeds or not" (the documentation).
            self.ole32.CoTaskMemFree(out)

    # ── windows ──

    def top_windows(self) -> list[Window]:
        """What the taskbar shows: visible, unowned, not a tool window, not cloaked, titled."""
        found: list[Window] = []

        def visit(hwnd: Any, _lparam: Any) -> bool:
            try:
                h = int(hwnd or 0)
                if h and self._is_app_window(h):
                    title = self._text(h)
                    if title:
                        pid = self.window_pid(h)
                        found.append(Window(h, title, pid, self.image_name(pid), self._class(h)))
            except Exception:  # noqa: BLE001 - a window closing mid-walk must not end the walk
                pass
            return True  # an exception escaping a ctypes callback would stop EnumWindows

        callback = self.enum_proc(visit)  # held until EnumWindows returns
        if not self.user32.EnumWindows(callback, 0):
            raise OSError(self.last_error(), "EnumWindows failed")
        return found

    def _is_app_window(self, h: int) -> bool:
        if not self.user32.IsWindowVisible(h) or self.user32.GetWindow(h, GW_OWNER):
            return False
        if self._window_long is not None and self._window_long(h, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return False
        cloaked = ctypes.c_uint32()
        hr = self.dwmapi.DwmGetWindowAttribute(
            h, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return not (hr == 0 and cloaked.value)

    def _text(self, h: int) -> str:
        n = self.user32.GetWindowTextLengthW(h)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self.user32.GetWindowTextW(h, buf, n + 1)
        return buf.value.strip()

    def _class(self, h: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(h, buf, 256)
        return buf.value

    def window_pid(self, h: int) -> int:
        pid = ctypes.c_uint32()
        self.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return int(pid.value)

    def image_name(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(32768)
            ok = self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            self.kernel32.CloseHandle(handle)

    def is_window(self, h: int) -> bool:
        return bool(self.user32.IsWindow(h))

    def post_close(self, h: int) -> None:
        """WM_CLOSE: exactly what the X button sends. The app may ask to save, or refuse."""
        if self.user32.PostMessageW(h, WM_CLOSE, 0, 0):
            return
        err = self.last_error()
        if err == ERROR_ACCESS_DENIED:
            # UIPI: a normal process may not post to an elevated one's window.
            raise PermissionError(err, "the window belongs to a program running as administrator")
        raise OSError(err, "PostMessageW failed")

    # ── keys, lock ──

    def send_keys(self, vks: Iterable[int]) -> None:
        """Press and release each key in turn. UIPI blocks silently; nothing reports it."""
        keys = list(vks)
        if not keys:
            return
        n = 2 * len(keys)
        events = (INPUT * n)()
        for i, vk in enumerate(keys):
            for j, flags in enumerate(
                (KEYEVENTF_EXTENDEDKEY, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)
            ):
                e = events[2 * i + j]
                e.type = INPUT_KEYBOARD
                e.ki.wVk = vk
                e.ki.dwFlags = flags
        sent = self.user32.SendInput(n, events, ctypes.sizeof(INPUT))
        if sent != n:
            raise OSError(self.last_error(), f"SendInput sent {sent} of {n} key events")

    def lock(self) -> None:
        # Asynchronous: nonzero means the lock was started, not that it finished.
        if not self.user32.LockWorkStation():
            raise OSError(self.last_error(), "LockWorkStation failed")

    # ── power ──

    def enable_shutdown_privilege(self) -> None:
        """SeShutdownPrivilege: held by every interactive user, enabled by nobody until asked."""
        token = ctypes.c_void_p()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise OSError(self.last_error(), "OpenProcessToken failed")
        try:
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            if not self.advapi32.LookupPrivilegeValueW(
                None, "SeShutdownPrivilege", ctypes.byref(tp.Privileges[0].Luid)
            ):
                raise OSError(self.last_error(), "LookupPrivilegeValueW failed")
            ok = self.advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
            # It "succeeds" without the privilege, saying so only in the last error.
            err = self.last_error()
            if not ok or err == ERROR_NOT_ALL_ASSIGNED:
                raise OSError(err or ERROR_NOT_ALL_ASSIGNED, "this account may not shut down")
        finally:
            self.kernel32.CloseHandle(token)

    def initiate_shutdown(self, *, restart: bool, message: str = "Jarvis") -> None:
        """Shut down or restart NOW, interactively: every app is asked, none is forced."""
        flags = SHUTDOWN_RESTART if restart else SHUTDOWN_POWEROFF
        err = self.advapi32.InitiateShutdownW(
            None, message, SHUTDOWN_GRACE_S, flags, SHTDN_REASON_FLAG_PLANNED
        )
        if err:
            raise OSError(err, f"InitiateShutdownW failed with {err}")

    def abort_shutdown(self) -> bool:
        """True when a pending shutdown was called off; False when none was pending."""
        self.enable_shutdown_privilege()
        if self.advapi32.AbortSystemShutdownW(None):
            return True
        err = self.last_error()
        if err == ERROR_NO_SHUTDOWN_IN_PROGRESS:
            return False
        raise OSError(err, "AbortSystemShutdownW failed")

    def suspend(self) -> None:
        # Real BOOLEANs through ctypes. `rundll32 powrprof.dll,SetSuspendState`
        # passes rundll32's own arguments in their place and can hibernate.
        if not self.powrprof.SetSuspendState(False, False, False):
            raise OSError(self.last_error(), "SetSuspendState failed")


# ───────────────────────────── Core Audio ─────────────────────────────


def _hr(value: int, what: str) -> None:
    if value < 0:
        raise OSError(f"{what} failed (HRESULT 0x{value & 0xFFFFFFFF:08X})")


class CoreAudio:
    """IAudioEndpointVolume on the default output device, through raw vtable calls.

    No comtypes (it generates code at run time, which a frozen exe cannot keep)
    and no pywin32 (it has no wrapper for these interfaces). Use it as a
    context manager, on a thread that called CoInitializeEx.
    """

    def __init__(self, ole32: Any, functype: Callable[..., Any]) -> None:
        self._ole32 = ole32
        self._functype = functype
        self._vol = ctypes.c_void_p()

    def _slot(self, obj: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return self._functype(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    def _release(self, obj: ctypes.c_void_p) -> None:
        if obj.value:
            self._slot(obj, SLOT_RELEASE, ctypes.c_uint32)(obj)
            obj.value = None

    def __enter__(self) -> CoreAudio:
        enum = ctypes.c_void_p()
        _hr(
            self._ole32.CoCreateInstance(
                ctypes.byref(GUID.parse(CLSID_MMDEVICE_ENUMERATOR)),
                None,
                CLSCTX_INPROC_SERVER,
                ctypes.byref(GUID.parse(IID_IMMDEVICE_ENUMERATOR)),
                ctypes.byref(enum),
            ),
            "CoCreateInstance(MMDeviceEnumerator)",
        )
        device = ctypes.c_void_p()
        try:
            get_default = self._slot(
                enum,
                SLOT_GET_DEFAULT_AUDIO_ENDPOINT,
                HRESULT,
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.POINTER(ctypes.c_void_p),
            )
            _hr(
                get_default(enum, E_RENDER, E_MULTIMEDIA, ctypes.byref(device)),
                "GetDefaultAudioEndpoint",
            )
            activate = self._slot(
                device,
                SLOT_ACTIVATE,
                HRESULT,
                ctypes.POINTER(GUID),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )
            _hr(
                activate(
                    device,
                    ctypes.byref(GUID.parse(IID_IAUDIO_ENDPOINT_VOLUME)),
                    CLSCTX_ALL,
                    None,
                    ctypes.byref(self._vol),
                ),
                "IMMDevice::Activate",
            )
        finally:
            self._release(device)
            self._release(enum)
        return self

    def __exit__(self, *_: object) -> None:
        self._release(self._vol)

    def read(self) -> Volume:
        level = ctypes.c_float()
        _hr(
            self._slot(self._vol, SLOT_GET_MASTER_SCALAR, HRESULT, ctypes.POINTER(ctypes.c_float))(
                self._vol, ctypes.byref(level)
            ),
            "GetMasterVolumeLevelScalar",
        )
        muted = ctypes.c_int32()
        _hr(
            self._slot(self._vol, SLOT_GET_MUTE, HRESULT, ctypes.POINTER(ctypes.c_int32))(
                self._vol, ctypes.byref(muted)
            ),
            "GetMute",
        )
        return Volume(level=round(level.value * 100), muted=bool(muted.value))

    def set_level(self, percent: int) -> None:
        scalar = min(100, max(0, int(percent))) / 100.0
        _hr(
            self._slot(self._vol, SLOT_SET_MASTER_SCALAR, HRESULT, ctypes.c_float, ctypes.c_void_p)(
                self._vol, scalar, None
            ),
            "SetMasterVolumeLevelScalar",
        )

    def set_mute(self, on: bool) -> None:
        _hr(
            self._slot(self._vol, SLOT_SET_MUTE, HRESULT, ctypes.c_int32, ctypes.c_void_p)(
                self._vol, int(bool(on)), None
            ),
            "SetMute",
        )


# ───────────────────────────── the COM thread ─────────────────────────────


class _Stuck(Exception):
    """The apartment did not answer in time."""


class _Apartment:
    """One daemon thread that called CoInitializeEx, running calls in order."""

    def __init__(self, init: Callable[[], None]) -> None:
        self._init = init
        self._queue: queue.SimpleQueue[
            tuple[Callable[[], Any], dict[str, Any], threading.Event]
        ] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def call(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._serve, name="jarvis-pc-com", daemon=True
                )
                self._thread.start()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._queue.put((fn, box, done))
        if not done.wait(timeout_s):
            raise _Stuck
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _serve(self) -> None:
        with contextlib.suppress(Exception):
            self._init()
        while True:
            fn, box, done = self._queue.get()
            try:
                box["value"] = fn()
            except Exception as exc:  # noqa: BLE001 - handed back to the caller, who words it
                box["error"] = exc
            finally:
                done.set()


def _timer(delay_s: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay_s, fn)
    t.daemon = True  # a countdown must never keep a quitting app alive
    t.start()


def _expand(value: str, env: Mapping[str, str]) -> str:
    return re.sub(r"%([^%]+)%", lambda m: env.get(m.group(1), m.group(0)), value)


def _app_path_target(value: object, env: Mapping[str, str]) -> str:
    """App Paths' (Default): a full path to an .exe, perhaps quoted, perhaps with %VARS%."""
    if not isinstance(value, str):
        return ""
    path = _expand(value.strip().strip('"').strip(), env)
    if not re.match(r"^[A-Za-z]:\\", path) or not path.casefold().endswith(".exe"):
        return ""
    return path


def _reason(exc: BaseException) -> str:
    text = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return str(text).rstrip(".")


# ───────────────────────────── the desktop ─────────────────────────────


class WindowsDesktop:
    """The Desktop for Windows. Cheap to build: nothing is loaded or read until asked."""

    backend = "windows"

    def __init__(
        self,
        *,
        api: Win32Api | None = None,
        startfile: Callable[[str], None] | None = None,
        run: Callable[..., Any] = subprocess.run,
        env: Mapping[str, str] | None = None,
        registry: Any = None,
        exists: Callable[[str], bool] = os.path.exists,
        walk: Callable[[str], Iterable[tuple[str, list[str], list[str]]]] = os.walk,
        after: Callable[[float, Callable[[], None]], None] = _timer,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        own_pids: Iterable[int] | None = None,
        own_exe: str | None = None,
        call_timeout_s: float = 20.0,
    ) -> None:
        self._api = api
        self._startfile = startfile or getattr(os, "startfile", None)
        self._run = run
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._registry = registry
        self._exists = exists
        self._walk = walk
        self._after = after
        self._clock = clock
        self._sleep = sleep
        self._own_pids = frozenset(own_pids if own_pids is not None else (os.getpid(),))
        # Frozen, sys.executable IS Jarvis.exe and every window it owns is ours. From
        # a venv it is python.exe, and the user's own Python windows are theirs.
        self._own_exe = (
            own_exe
            if own_exe is not None
            else (sys.executable if getattr(sys, "frozen", False) else "")
        )
        self.call_timeout_s = call_timeout_s
        self._lock = threading.Lock()
        self._apartment: _Apartment | None = None
        catalog_kwargs: dict[str, Any] = {"clock": clock}
        if spawn is not None:
            catalog_kwargs["spawn"] = spawn
        self.catalog = Catalog(
            {
                "startapps": self._start_apps,
                "startmenu": self._start_menu,
                "apppaths": self._app_paths,
                "settings": settings_apps,
            },
            **catalog_kwargs,
        )

    # ── plumbing ──

    @property
    def api(self) -> Win32Api:
        with self._lock:
            if self._api is None:
                self._api = Win32Api()
            return self._api

    def _com(self, fn: Callable[[], Any]) -> Any:
        with self._lock:
            if self._apartment is None:
                self._apartment = _Apartment(lambda: self.api.co_init())
            apartment = self._apartment
        try:
            return apartment.call(fn, self.call_timeout_s)
        except _Stuck:
            with self._lock:
                if self._apartment is apartment:
                    self._apartment = None  # abandoned; the next call gets a fresh thread
            raise PcRefused("Windows didn't answer in time, so I stopped waiting.") from None

    def status(self) -> PcStatus:
        if self.catalog.loaded:
            detail = f"Windows; {len(self.catalog.apps())} apps and settings pages known"
            if self.catalog.errors:
                detail += "; unread: " + ", ".join(sorted(self.catalog.errors))
        else:
            detail = "Windows; the app list is read on first use"
        return PcStatus(available=True, backend=self.backend, detail=detail)

    # ── the catalog ──

    def warm(self) -> None:
        self.catalog.warm()

    def apps(self) -> tuple[App, ...]:
        return self.catalog.apps()

    def rescan(self) -> tuple[App, ...]:
        return self.catalog.rescan()

    def _start_menu(self) -> list[App]:
        roots: list[str] = []
        for key, base in _PROGRAMS_FALLBACK:
            try:
                roots.append(self.api.known_folder(KNOWN_FOLDERS[key]))
            except OSError:
                if self._env.get(base):
                    roots.append(os.path.join(self._env[base], *_START_MENU))
        return scan_start_menu([r for r in roots if self._exists(r)], walk=self._walk)

    def _app_paths(self) -> list[App]:
        reg = self._registry or importlib.import_module("winreg")
        out: list[App] = []
        for hive in (reg.HKEY_CURRENT_USER, reg.HKEY_LOCAL_MACHINE):
            for view in (reg.KEY_WOW64_64KEY, reg.KEY_WOW64_32KEY):
                try:
                    key = reg.OpenKey(hive, _APP_PATHS, 0, reg.KEY_READ | view)
                except OSError:
                    continue
                with key:
                    for i in itertools.count():
                        try:
                            sub = reg.EnumKey(key, i)
                        except OSError:
                            break
                        try:
                            with reg.OpenKey(key, sub, 0, reg.KEY_READ | view) as k:
                                value, _type = reg.QueryValueEx(k, "")
                        except OSError:
                            continue
                        target = _app_path_target(value, self._env)
                        # A stale entry from an uninstalled app would match and then fail.
                        if target and self._exists(target):
                            stem = sub[:-4] if sub.casefold().endswith(".exe") else sub
                            out.append(App(name=stem, target=target, source="apppaths"))
        return out

    def _start_apps(self) -> list[App]:
        root = self._env.get("SystemRoot") or self._env.get("SYSTEMROOT") or r"C:\Windows"
        shell = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        fd, out = tempfile.mkstemp(prefix="jarvis-apps-", suffix=".json")
        os.close(fd)
        try:
            proc = self._run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", _STARTAPPS_SCRIPT],
                env={**self._env, _STARTAPPS_OUT: out},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=STARTAPPS_TIMEOUT_S,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
            if proc.returncode != 0:
                why = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
                raise OSError(f"Get-StartApps exited {proc.returncode}: {why}")
            with open(out, encoding="utf-8-sig") as fh:
                return parse_start_apps(fh.read())
        finally:
            with contextlib.suppress(OSError):
                os.unlink(out)

    # ── opening things ──

    def _open(self, target: str) -> None:
        if self._startfile is None:
            raise PcRefused("I can't open things on this computer.")
        start = self._startfile
        self._com(lambda: start(target))

    def launch(self, app: App) -> None:
        if not launchable(app) or app not in self.apps():
            raise PcRefused("I can only open apps I found installed on this computer.")
        try:
            self._open(app.target)
        except OSError as exc:
            raise PcRefused(f"Windows couldn't start {app.spoken}: {_reason(exc)}.") from exc

    def open_url(self, url: str) -> None:
        safe = safe_url(url)
        try:
            self._open(safe)
        except OSError as exc:
            raise PcRefused(f"Windows couldn't open the browser: {_reason(exc)}.") from exc

    def open_path(self, path: Path) -> None:
        if not path.exists():
            raise PcRefused(f"I can't find {path.name}.")
        if path.is_file() and is_executable(path.name, self._env.get("PATHEXT", "")):
            raise PcRefused("I won't open programs, scripts or shortcuts from a folder.")
        try:
            self._open(str(path))
        except OSError as exc:
            raise PcRefused(f"Windows couldn't open {path.name}: {_reason(exc)}.") from exc

    def known_folder(self, place: str) -> Path:
        guid = KNOWN_FOLDERS.get(place)
        if guid is None or place in ("programs", "common_programs"):
            raise PcRefused(f"I don't know a folder called {place}.")
        try:
            return Path(self.api.known_folder(guid))
        except OSError:
            profile = self._env.get("USERPROFILE", "")
            guess = Path(profile, _FOLDER_NAMES.get(place, "")) if profile else None
            if guess is not None and guess.is_dir():
                return guess
            raise PcRefused(f"Windows wouldn't tell me where your {place} folder is.") from None

    # ── windows ──

    def windows(self) -> tuple[Window, ...]:
        try:
            raw = self.api.top_windows()
        except OSError as exc:
            raise PcRefused(f"I couldn't list the open windows: {_reason(exc)}.") from exc
        own = self._own_exe.casefold()
        return tuple(
            Window(
                w.hwnd,
                w.title,
                w.pid,
                w.exe,
                w.cls,
                own=w.pid in self._own_pids or bool(own and w.exe.casefold() == own),
            )
            for w in raw
        )

    def close(self, window: Window) -> None:
        try:
            self.api.post_close(window.hwnd)
        except PermissionError:
            raise PcRefused(
                f"{window.title} is running as administrator, so Windows won't let me close it."
            ) from None
        except OSError as exc:
            raise PcRefused(f"Windows wouldn't pass the close request on: {_reason(exc)}.") from exc

    def wait_closed(self, window: Window, timeout_s: float) -> bool:
        deadline = self._clock() + timeout_s
        while True:
            # A reused handle is a different window: gone means gone or not that pid.
            if (
                not self.api.is_window(window.hwnd)
                or self.api.window_pid(window.hwnd) != window.pid
            ):
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(0.25)

    # ── sound ──

    def _audio(self, op: Callable[[CoreAudio], Volume]) -> Volume:
        def work() -> Volume:
            with CoreAudio(self.api.ole32, self.api.functype) as audio:
                return op(audio)

        result: Volume = self._com(work)
        return result

    def volume(self) -> Volume | None:
        try:
            return self._audio(lambda a: a.read())
        except OSError:
            return None

    def set_volume(self, level: int) -> Volume | None:
        level = min(100, max(0, int(level)))

        def op(a: CoreAudio) -> Volume:
            a.set_level(level)
            if level > 0:
                a.set_mute(False)
            return a.read()

        try:
            return self._audio(op)
        except OSError:
            # No Core Audio: fifty presses down reach zero from anywhere, then up
            # in the keys' own steps. Not read back, so it is reported as "about".
            self._keys([VK_VOLUME_DOWN] * 50 + [VK_VOLUME_UP] * round(level / 2))
            return Volume(level=level, muted=False, exact=False)

    def step_volume(self, delta: int) -> Volume | None:
        def op(a: CoreAudio) -> Volume:
            now = a.read()
            a.set_level(now.level + delta)
            if delta > 0 and now.muted:
                a.set_mute(False)
            return a.read()

        try:
            return self._audio(op)
        except OSError:
            presses = max(1, round(abs(delta) / 2))
            self._keys([VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN] * presses)
            return None

    def set_mute(self, on: bool) -> Volume | None:
        def op(a: CoreAudio) -> Volume:
            a.set_mute(on)
            return a.read()

        try:
            return self._audio(op)
        except OSError:
            # The mute key TOGGLES; up-then-down is the only key sequence that is
            # certain to leave the sound on.
            self._keys([VK_VOLUME_MUTE] if on else [VK_VOLUME_UP, VK_VOLUME_DOWN])
            return None

    def _keys(self, vks: list[int]) -> None:
        try:
            self.api.send_keys(vks)
        except OSError as exc:
            raise PcRefused(f"Windows wouldn't take the key presses: {_reason(exc)}.") from exc

    def media(self, action: str) -> None:
        vk = MEDIA_KEYS.get(action)
        if vk is None:
            raise PcRefused("I can play or pause, skip, go back or stop.")
        self._keys([vk])

    # ── session and power ──

    def lock(self) -> None:
        try:
            self.api.lock()
        except OSError as exc:
            raise PcRefused(f"Windows wouldn't lock: {_reason(exc)}.") from exc

    def power(self, action: str) -> None:
        if action not in ("shutdown", "restart"):
            raise PcRefused("I can shut down or restart.")
        try:
            self.api.enable_shutdown_privilege()
            self.api.initiate_shutdown(restart=action == "restart")
        except OSError as exc:
            if exc.errno == ERROR_SHUTDOWN_IN_PROGRESS:
                raise PcRefused("Windows is already shutting down.") from exc
            verb = "shut down" if action == "shutdown" else "restart"
            raise PcRefused(f"Windows wouldn't {verb}: {_reason(exc)}.") from exc

    def sleep(self) -> None:
        try:
            self.api.enable_shutdown_privilege()
            self.api.suspend()
        except OSError as exc:
            raise PcRefused(f"Windows wouldn't go to sleep: {_reason(exc)}.") from exc

    def cancel_power(self) -> bool:
        try:
            return self.api.abort_shutdown()
        except OSError as exc:
            raise PcRefused(f"Windows wouldn't call it off: {_reason(exc)}.") from exc

    def after(self, delay_s: float, fn: Callable[[], None]) -> None:
        self._after(delay_s, fn)
