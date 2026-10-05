"""The Windows backend, driven on Linux with fake DLLs that record every call.

What these pin is the part that fails SILENTLY on Windows: a struct one field
short (SendInput then does nothing and says nothing), a shutdown flag that
forces apps closed, a privilege never enabled, a COM vtable slot off by one, a
PowerShell window flashing over the user's work. The fake COM objects are real
C function pointers in a real vtable, so the slot numbers and argument types
are exercised through ctypes exactly as on Windows; their slot numbers are
written here from the interface definitions, independently of the module's.
"""

from __future__ import annotations

import ast
import ctypes
import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis.pc import windows as W
from jarvis.pc.base import App, PcRefused, Volume

ROOT = Path(__file__).resolve().parents[1]
WIN64 = ctypes.sizeof(ctypes.c_void_p) == 8

# ───────────────────────────── structs ─────────────────────────────


@pytest.mark.skipif(not WIN64, reason="the Win64 sizes; Win32's INPUT is 28")
def test_input_is_the_win64_size_sendinput_demands() -> None:
    assert ctypes.sizeof(W.MOUSEINPUT) == 32
    assert ctypes.sizeof(W.KEYBDINPUT) == 24
    assert ctypes.sizeof(W.INPUT) == 40


def test_leaving_mouseinput_out_of_the_union_would_break_sendinput() -> None:
    # The mistake this guards: "it's only the keyboard", and SendInput rejects
    # every call because cbSize is not sizeof(INPUT). Nothing reports it.
    class KeyboardOnly(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("ki", W.KEYBDINPUT)]

    assert ctypes.sizeof(KeyboardOnly) < ctypes.sizeof(W.INPUT)


def test_a_guid_is_laid_out_as_windows_reads_it() -> None:
    g = W.GUID.parse(W.KNOWN_FOLDERS["downloads"])
    assert (g.Data1, g.Data2, g.Data3) == (0x374DE290, 0x123F, 0x4565)
    assert bytes(g.Data4).hex() == "916439c4925e467b"


# ───────────────────────────── fake DLLs ─────────────────────────────


class Err:
    value = 0


class FakeUser32:
    def __init__(self, err: Err) -> None:
        self.err = err
        self.windows: dict[int, dict[str, Any]] = {}
        self.posted: list[tuple[int, int]] = []
        self.post_error = 0
        self.inputs: list[tuple[int, int]] = []
        self.cb_size = 0
        self.short_by = 0
        self.locked = 0

    def add(self, hwnd: int, title: str, pid: int, **kw: Any) -> None:
        self.windows[hwnd] = {"title": title, "pid": pid, "visible": 1, "owner": 0,
                              "exstyle": 0, "cls": "Notepad", **kw}  # fmt: skip

    def EnumWindows(self, proc: Callable[[int, int], bool], lparam: int) -> int:  # noqa: N802
        for h in list(self.windows):
            if not proc(h, lparam):
                break
        return 1

    def IsWindowVisible(self, h: int) -> int:  # noqa: N802
        return self.windows[h]["visible"]

    def IsWindow(self, h: int) -> int:  # noqa: N802
        return int(h in self.windows)

    def GetWindow(self, h: int, cmd: int) -> int:  # noqa: N802
        assert cmd == W.GW_OWNER
        return self.windows[h]["owner"]

    def GetWindowLongPtrW(self, h: int, index: int) -> int:  # noqa: N802
        assert index == W.GWL_EXSTYLE
        return self.windows[h]["exstyle"]

    def GetWindowTextLengthW(self, h: int) -> int:  # noqa: N802
        return len(self.windows[h]["title"])

    def GetWindowTextW(self, h: int, buf: Any, n: int) -> int:  # noqa: N802
        buf.value = self.windows[h]["title"][: n - 1]
        return len(buf.value)

    def GetClassNameW(self, h: int, buf: Any, n: int) -> int:  # noqa: N802
        buf.value = self.windows[h]["cls"]
        return len(buf.value)

    def GetWindowThreadProcessId(self, h: int, ref: Any) -> int:  # noqa: N802
        ref._obj.value = self.windows[h]["pid"] if h in self.windows else 0
        return 1

    def PostMessageW(self, h: int, msg: int, wp: int, lp: int) -> int:  # noqa: N802
        if self.post_error:
            self.err.value = self.post_error
            return 0
        self.posted.append((h, msg))
        return 1

    def SendInput(self, n: int, events: Any, size: int) -> int:  # noqa: N802
        self.cb_size = size
        self.inputs += [(events[i].ki.wVk, events[i].ki.dwFlags) for i in range(n)]
        assert all(events[i].type == W.INPUT_KEYBOARD for i in range(n))
        return n - self.short_by

    def LockWorkStation(self) -> int:  # noqa: N802
        self.locked += 1
        return 1


class FakeDwm:
    def __init__(self) -> None:
        self.cloaked: set[int] = set()

    def DwmGetWindowAttribute(self, h: int, attr: int, ref: Any, size: int) -> int:  # noqa: N802
        assert attr == W.DWMWA_CLOAKED and size == 4
        ref._obj.value = int(h in self.cloaked)
        return 0


class FakeKernel32:
    def __init__(self) -> None:
        self.exes: dict[int, str] = {}
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802
        assert access == W.PROCESS_QUERY_LIMITED_INFORMATION
        return pid + 5000 if pid in self.exes else 0

    def QueryFullProcessImageNameW(self, h: int, flags: int, buf: Any, size: Any) -> int:  # noqa: N802
        buf.value = self.exes[h - 5000]
        return 1

    def CloseHandle(self, h: Any) -> int:  # noqa: N802
        self.closed.append(h if isinstance(h, int) else h.value)
        return 1

    def GetCurrentProcess(self) -> int:  # noqa: N802
        return -1


class FakeAdvapi32:
    def __init__(self, err: Err, calls: list[str]) -> None:
        self.err = err
        self.calls = calls
        self.adjust_error = 0
        self.initiate_result = 0
        self.abort_ok = True
        self.abort_error = 0
        self.shutdowns: list[tuple[Any, ...]] = []
        self.privilege = ""
        self.enabled = 0

    def OpenProcessToken(self, proc: int, access: int, ref: Any) -> int:  # noqa: N802
        assert proc == -1 and access & W.TOKEN_ADJUST_PRIVILEGES
        ref._obj.value = 77
        return 1

    def LookupPrivilegeValueW(self, system: Any, name: str, ref: Any) -> int:  # noqa: N802
        self.privilege = name
        ref._obj.LowPart = 19
        return 1

    def AdjustTokenPrivileges(self, tok: Any, disable: bool, ref: Any, *rest: Any) -> int:  # noqa: N802
        tp = ref._obj
        self.enabled = tp.Privileges[0].Attributes if tp.Privileges[0].Luid.LowPart == 19 else -1
        self.calls.append("privilege")
        self.err.value = self.adjust_error
        return 1

    def InitiateShutdownW(self, *args: Any) -> int:  # noqa: N802
        self.calls.append("shutdown")
        self.shutdowns.append(args)
        return self.initiate_result

    def AbortSystemShutdownW(self, machine: Any) -> int:  # noqa: N802
        self.calls.append("abort")
        self.err.value = self.abort_error
        return int(self.abort_ok)


class FakeShell32:
    def __init__(self, folders: dict[str, str]) -> None:
        self.folders = folders
        self.asked: list[str] = []

    def SHGetKnownFolderPath(self, guid_ref: Any, flags: int, token: Any, out: Any) -> int:  # noqa: N802
        g = guid_ref._obj
        text = str(W.uuid.UUID(bytes_le=bytes(g))).upper()
        self.asked.append(text)
        path = self.folders.get("{" + text + "}")
        if path is None:
            return -2147024894  # 0x80070002, not found
        out._obj.value = path
        return 0


class FakeComObject:
    """A COM object in memory: a pointer to a table of real C function pointers."""

    def __init__(self, slots: dict[int, tuple[Any, list[Any], Callable[..., Any]]]) -> None:
        self._keep = []
        table = (ctypes.c_void_p * 24)()
        for index, (restype, argtypes, fn) in slots.items():
            cfn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)
            self._keep.append(cfn)
            table[index] = ctypes.cast(cfn, ctypes.c_void_p).value
        self._table = table
        self._obj = (ctypes.c_void_p * 1)(ctypes.addressof(table))
        self.address = ctypes.addressof(self._obj)


class FakeEndpoint:
    """MMDeviceEnumerator -> IMMDevice -> IAudioEndpointVolume, slots from the IDL."""

    def __init__(self) -> None:
        self.level = 0.5
        self.muted = 0
        self.released: list[str] = []
        self.role: tuple[int, int] | None = None
        self.activated_iid = ""
        hr, ulong = ctypes.c_int32, ctypes.c_uint32

        def release(name: str) -> Callable[[Any], int]:
            def fn(this: Any) -> int:
                self.released.append(name)
                return 0

            return fn

        def get_level(this: Any, out: Any) -> int:
            out[0] = self.level
            return 0

        def set_level(this: Any, value: float, ctx: Any) -> int:
            self.level = value
            return 0

        def get_mute(this: Any, out: Any) -> int:
            out[0] = self.muted
            return 0

        def set_mute(this: Any, value: int, ctx: Any) -> int:
            self.muted = value
            return 0

        self.volume = FakeComObject(
            {
                2: (ulong, [], release("volume")),
                7: (hr, [ctypes.c_float, ctypes.c_void_p], set_level),  # SetMasterVolumeLevelScalar
                9: (hr, [ctypes.POINTER(ctypes.c_float)], get_level),  # GetMasterVolumeLevelScalar
                14: (hr, [ctypes.c_int32, ctypes.c_void_p], set_mute),  # SetMute
                15: (hr, [ctypes.POINTER(ctypes.c_int32)], get_mute),  # GetMute
            }
        )

        def activate(this: Any, iid: Any, ctx: int, params: Any, out: Any) -> int:
            self.activated_iid = str(W.uuid.UUID(bytes_le=bytes(iid.contents))).upper()
            out[0] = self.volume.address
            return 0

        self.device = FakeComObject(
            {
                2: (ulong, [], release("device")),
                3: (
                    hr,
                    [
                        ctypes.POINTER(W.GUID),
                        ctypes.c_uint32,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p),
                    ],
                    activate,
                ),  # fmt: skip
            }
        )

        def default_endpoint(this: Any, flow: int, role: int, out: Any) -> int:
            self.role = (flow, role)
            out[0] = self.device.address
            return 0

        self.enumerator = FakeComObject(
            {
                2: (ulong, [], release("enumerator")),
                4: (
                    hr,
                    [ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)],
                    default_endpoint,
                ),
            }
        )


class FakeOle32:
    def __init__(self, endpoint: FakeEndpoint | None) -> None:
        self.endpoint = endpoint
        self.freed = 0
        self.inits: list[int] = []

    def CoInitializeEx(self, reserved: Any, flags: int) -> int:  # noqa: N802
        assert flags == W.COINIT_APARTMENTTHREADED | W.COINIT_DISABLE_OLE1DDE
        self.inits.append(threading.get_ident())
        return 0

    def CoTaskMemFree(self, p: Any) -> None:  # noqa: N802
        self.freed += 1

    def CoCreateInstance(self, clsid: Any, outer: Any, ctx: int, iid: Any, out: Any) -> int:  # noqa: N802
        if self.endpoint is None:
            return -2147221164  # REGDB_E_CLASSNOTREG: no Core Audio here
        assert bytes(clsid._obj) == bytes(W.GUID.parse(W.CLSID_MMDEVICE_ENUMERATOR))
        assert bytes(iid._obj) == bytes(W.GUID.parse(W.IID_IMMDEVICE_ENUMERATOR))
        out._obj.value = self.endpoint.enumerator.address
        return 0


class FakePowrprof:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.args: tuple[Any, ...] = ()

    def SetSuspendState(self, *args: Any) -> int:  # noqa: N802
        self.calls.append("suspend")
        self.args = args
        return 1


class Fakes:
    def __init__(self, *, audio: bool = True, folders: dict[str, str] | None = None) -> None:
        self.err = Err()
        self.calls: list[str] = []
        self.user32 = FakeUser32(self.err)
        self.dwm = FakeDwm()
        self.kernel32 = FakeKernel32()
        self.advapi32 = FakeAdvapi32(self.err, self.calls)
        self.shell32 = FakeShell32(folders or {})
        self.endpoint = FakeEndpoint() if audio else None
        self.ole32 = FakeOle32(self.endpoint)
        self.powrprof = FakePowrprof(self.calls)

    def api(self) -> W.Win32Api:
        return W.Win32Api(
            user32=self.user32,
            shell32=self.shell32,
            ole32=self.ole32,
            kernel32=self.kernel32,
            advapi32=self.advapi32,
            dwmapi=self.dwm,
            powrprof=self.powrprof,
            enum_proc=lambda fn: fn,
            functype=ctypes.CFUNCTYPE,
            last_error=lambda: self.err.value,
        )


def desktop(fakes: Fakes, **kw: Any) -> W.WindowsDesktop:
    started: list[str] = []
    kw.setdefault("startfile", started.append)
    d = W.WindowsDesktop(api=fakes.api(), env=kw.pop("env", {}), **kw)
    d.started = started  # type: ignore[attr-defined]
    return d


# ───────────────────────────── keys ─────────────────────────────


def test_each_key_is_pressed_and_released_with_the_right_struct_size() -> None:
    f = Fakes()
    d = desktop(f)
    d.media("play_pause")
    ext, up = W.KEYEVENTF_EXTENDEDKEY, W.KEYEVENTF_KEYUP
    assert f.user32.inputs == [(0xB3, ext), (0xB3, ext | up)]
    assert f.user32.cb_size == ctypes.sizeof(W.INPUT)


@pytest.mark.parametrize(("action", "vk"), [("next", 0xB0), ("previous", 0xB1), ("stop", 0xB2)])
def test_media_keys(action: str, vk: int) -> None:
    f = Fakes()
    desktop(f).media(action)
    assert [k for k, _ in f.user32.inputs] == [vk, vk]


def test_a_refused_sendinput_is_a_sentence_not_silence() -> None:
    f = Fakes()
    f.user32.short_by = 1
    with pytest.raises(PcRefused, match="key presses"):
        desktop(f).media("next")


# ───────────────────────────── volume ─────────────────────────────


def test_core_audio_sets_reads_back_and_releases_everything() -> None:
    f = Fakes()
    f.endpoint.muted = 1  # type: ignore[union-attr]
    got = desktop(f).set_volume(30)
    ep = f.endpoint
    assert ep is not None
    assert got == Volume(level=30, muted=False)
    assert abs(ep.level - 0.30) < 1e-6 and ep.muted == 0  # setting a level unmutes
    assert ep.role == (W.E_RENDER, W.E_MULTIMEDIA)
    assert ep.activated_iid == W.IID_IAUDIO_ENDPOINT_VOLUME.strip("{}")
    assert sorted(ep.released) == ["device", "enumerator", "volume"]


def test_core_audio_steps_from_where_it_is() -> None:
    f = Fakes()
    d = desktop(f)
    assert d.volume() == Volume(level=50, muted=False)
    assert d.step_volume(-20) == Volume(level=30, muted=False)
    assert d.step_volume(90) == Volume(level=100, muted=False)
    assert d.set_mute(True) == Volume(level=100, muted=True)


def test_com_runs_on_one_thread_that_initialised_it_once() -> None:
    f = Fakes()
    d = desktop(f)
    seen: list[int] = []
    for _ in range(3):
        d.volume()
        d._com(lambda: seen.append(threading.get_ident()))
    assert len(f.ole32.inits) == 1 and set(seen) == {f.ole32.inits[0]}
    assert f.ole32.inits[0] != threading.get_ident()


def test_without_core_audio_the_volume_keys_are_the_fallback() -> None:
    f = Fakes(audio=False)
    d = desktop(f)
    assert d.volume() is None
    assert d.set_volume(30) == Volume(level=30, muted=False, exact=False)
    downs = [k for k, fl in f.user32.inputs if k == W.VK_VOLUME_DOWN and not fl & W.KEYEVENTF_KEYUP]
    ups = [k for k, fl in f.user32.inputs if k == W.VK_VOLUME_UP and not fl & W.KEYEVENTF_KEYUP]
    assert (len(downs), len(ups)) == (50, 15)
    f.user32.inputs.clear()
    assert d.step_volume(10) is None
    assert [k for k, fl in f.user32.inputs if not fl & W.KEYEVENTF_KEYUP] == [W.VK_VOLUME_UP] * 5
    f.user32.inputs.clear()
    d.set_mute(False)  # the mute key toggles; up then down always leaves sound on
    assert [k for k, fl in f.user32.inputs if not fl & W.KEYEVENTF_KEYUP] == [
        W.VK_VOLUME_UP,
        W.VK_VOLUME_DOWN,
    ]


def test_a_stuck_com_call_is_abandoned_and_the_next_gets_a_fresh_thread() -> None:
    f = Fakes()
    d = desktop(f, call_timeout_s=0.2)
    release = threading.Event()
    with pytest.raises(PcRefused, match="didn't answer"):
        d._com(lambda: release.wait(5))
    try:
        assert d._com(lambda: "ok") == "ok"
        assert len(f.ole32.inits) == 2
    finally:
        release.set()


# ───────────────────────────── power ─────────────────────────────


@pytest.mark.parametrize(("action", "flag"), [("shutdown", 0x8), ("restart", 0x4)])
def test_a_shutdown_is_asked_for_now_with_no_force_flag(action: str, flag: int) -> None:
    f = Fakes()
    desktop(f).power(action)
    assert f.calls == ["privilege", "shutdown"]  # the privilege is enabled FIRST
    assert f.advapi32.privilege == "SeShutdownPrivilege"
    assert f.advapi32.enabled == W.SE_PRIVILEGE_ENABLED
    ((machine, _message, grace, flags, reason),) = f.advapi32.shutdowns
    assert machine is None and grace == 0
    assert flags == flag and not flags & W.FORCE_FLAGS
    assert not flags & (0x1 | 0x2 | 0x20)  # FORCE_OTHERS, FORCE_SELF, GRACE_OVERRIDE
    assert reason & 0x80000000  # planned


def test_no_flag_constant_that_forces_reaches_initiate_shutdown() -> None:
    # Belt and braces over the call above: nowhere in the module is a force flag
    # or shutdown.exe (whose /t implies /f) passed to anything.
    tree = ast.parse((ROOT / "jarvis/pc/windows.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "InitiateShutdownW":
            names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
            assert not names & {"SHUTDOWN_FORCE_SELF", "SHUTDOWN_FORCE_OTHERS",
                                "SHUTDOWN_GRACE_OVERRIDE", "FORCE_FLAGS"}  # fmt: skip
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.casefold() not in ("shutdown.exe", "/t", "/f"), node.value
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant):
            assert str(node.elts[0].value).casefold() != "shutdown", "an argv runs shutdown.exe"


def test_a_missing_privilege_is_a_sentence() -> None:
    f = Fakes()
    f.advapi32.adjust_error = W.ERROR_NOT_ALL_ASSIGNED
    with pytest.raises(PcRefused, match="wouldn't shut down"):
        desktop(f).power("shutdown")
    assert "shutdown" not in f.calls


def test_a_shutdown_already_under_way_is_said_so() -> None:
    f = Fakes()
    f.advapi32.initiate_result = W.ERROR_SHUTDOWN_IN_PROGRESS
    with pytest.raises(PcRefused, match="already shutting down"):
        desktop(f).power("restart")


def test_abort_reports_whether_anything_was_pending() -> None:
    f = Fakes()
    d = desktop(f)
    assert d.cancel_power() is True
    f.advapi32.abort_ok, f.advapi32.abort_error = False, W.ERROR_NO_SHUTDOWN_IN_PROGRESS
    assert d.cancel_power() is False
    f.advapi32.abort_error = W.ERROR_ACCESS_DENIED
    with pytest.raises(PcRefused):
        d.cancel_power()


def test_sleep_is_real_booleans_after_the_privilege() -> None:
    f = Fakes()
    desktop(f).sleep()
    assert f.calls == ["privilege", "suspend"]
    assert f.powrprof.args == (False, False, False)


# ───────────────────────────── windows ─────────────────────────────


def test_only_what_the_taskbar_shows_is_listed() -> None:
    f = Fakes()
    u = f.user32
    u.add(1, "notes.txt - Notepad", 100)
    u.add(2, "hidden", 101, visible=0)
    u.add(3, "a dialog", 102, owner=1)
    u.add(4, "a palette", 103, exstyle=W.WS_EX_TOOLWINDOW)
    u.add(5, "suspended UWP", 104)
    u.add(6, "", 105)
    u.add(7, "Jarvis", 4242, cls="Chrome_WidgetWin_1")
    f.dwm.cloaked.add(5)
    f.kernel32.exes.update({100: r"C:\Windows\notepad.exe", 4242: r"C:\Jarvis\Jarvis.exe"})
    got = desktop(f, own_pids=[4242]).windows()
    assert [(w.hwnd, w.title, w.exe, w.own) for w in got] == [
        (1, "notes.txt - Notepad", r"C:\Windows\notepad.exe", False),
        (7, "Jarvis", r"C:\Jarvis\Jarvis.exe", True),
    ]
    assert sorted(f.kernel32.closed) == [5100, 9242]  # every process handle closed


def test_the_frozen_exe_owns_its_windows_whatever_the_pid() -> None:
    f = Fakes()
    f.user32.add(1, "J.A.R.V.I.S.", 9)
    f.kernel32.exes[9] = r"C:\Program Files\Jarvis\Jarvis.exe"
    (w,) = desktop(f, own_pids=[], own_exe=r"c:\program files\jarvis\JARVIS.EXE").windows()
    assert w.own


def test_close_posts_wm_close_and_never_anything_stronger() -> None:
    f = Fakes()
    f.user32.add(1, "notes.txt - Notepad", 100)
    d = desktop(f)
    (w,) = d.windows()
    d.close(w)
    assert f.user32.posted == [(1, W.WM_CLOSE)]


def test_an_elevated_window_is_refused_by_name() -> None:
    f = Fakes()
    f.user32.add(1, "Task Manager", 100)
    f.user32.post_error = W.ERROR_ACCESS_DENIED
    d = desktop(f)
    (w,) = d.windows()
    with pytest.raises(PcRefused, match="Task Manager is running as administrator"):
        d.close(w)


def test_waiting_for_a_window_to_close() -> None:
    f = Fakes()
    f.user32.add(1, "notes.txt - Notepad", 100)
    now = [0.0]

    def tick(s: float) -> None:
        now[0] += s

    d = desktop(f, clock=lambda: now[0], sleep=tick)
    (w,) = d.windows()
    assert d.wait_closed(w, 1.0) is False and now[0] >= 1.0
    del f.user32.windows[1]
    assert d.wait_closed(w, 1.0) is True


# ───────────────────────────── folders ─────────────────────────────


def test_known_folders_come_from_the_shell_and_are_always_freed(tmp_path: Path) -> None:
    docs = str(tmp_path / "OneDrive" / "Belgeler")
    f = Fakes(folders={W.KNOWN_FOLDERS["documents"]: docs})
    d = desktop(f, env={"USERPROFILE": str(tmp_path)})
    assert d.known_folder("documents") == Path(docs)  # OneDrive's, not %USERPROFILE%\Documents
    assert f.ole32.freed == 1
    (tmp_path / "Music").mkdir()
    assert d.known_folder("music") == tmp_path / "Music"  # the shell failed; the default exists
    assert f.ole32.freed == 2  # freed on failure too, as the documentation insists
    with pytest.raises(PcRefused):
        d.known_folder("videos")
    with pytest.raises(PcRefused):
        d.known_folder("programs")


# ───────────────────────────── the catalog ─────────────────────────────


class FakeKey:
    def __init__(self, subkeys: dict[str, Any]) -> None:
        self.subkeys = subkeys

    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *_: object) -> None:
        pass


class FakeWinreg:
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 0x20019, 0x100, 0x200

    def __init__(self, tree: dict[tuple[str, int], dict[str, Any]]) -> None:
        self.tree = tree
        self.opened: list[Any] = []

    def OpenKey(self, parent: Any, sub: str, reserved: int = 0, access: int = 0) -> FakeKey:  # noqa: N802
        self.opened.append((parent if isinstance(parent, str) else "key", sub, access))
        if isinstance(parent, FakeKey):
            if sub not in parent.subkeys:
                raise OSError("no such key")
            return FakeKey({"": parent.subkeys[sub]})
        view = access & (self.KEY_WOW64_64KEY | self.KEY_WOW64_32KEY)
        found = self.tree.get((parent, view))
        if found is None:
            raise OSError("no such key")
        return FakeKey(found)

    def EnumKey(self, key: FakeKey, i: int) -> str:  # noqa: N802
        names = list(key.subkeys)
        if i >= len(names):
            raise OSError("no more")
        return names[i]

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[Any, int]:  # noqa: N802
        value = key.subkeys[name]
        if value is None:
            raise OSError("no default value")
        return value, 1


def test_app_paths_are_read_from_both_hives_and_both_views() -> None:
    reg = FakeWinreg(
        {
            ("HKLM", 0x100): {
                "chrome.exe": '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"',
                "gone.exe": "C:\\Old\\gone.exe",
                "relative.exe": "relative.exe",
            },
            ("HKLM", 0x200): {
                "wordpad.exe": "%ProgramFiles%\\Windows NT\\Accessories\\wordpad.exe"
            },
            ("HKCU", 0x100): {"nodefault.exe": None},
        }
    )
    present = {
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\PF\\Windows NT\\Accessories\\wordpad.exe",
    }
    d = desktop(Fakes(), registry=reg, exists=present.__contains__, env={"ProgramFiles": "C:\\PF"})
    got = sorted((a.name, a.target) for a in d._app_paths())
    assert got == [
        ("chrome", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        ("wordpad", "C:\\PF\\Windows NT\\Accessories\\wordpad.exe"),
    ]


def test_startapps_runs_powershell_hidden_and_reads_utf8_from_a_file() -> None:
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(argv=argv, **kw)
        rows = [
            {"Name": "Hesap Makinesi", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
            {"Name": "Görev Yöneticisi", "AppID": "taskmgr"},
        ]
        # Windows PowerShell 5.1's `Out-File -Encoding utf8` writes a BOM.
        Path(kw["env"]["JARVIS_STARTAPPS_OUT"]).write_bytes(
            b"\xef\xbb\xbf" + json.dumps(rows, ensure_ascii=False).encode("utf-8")
        )
        return subprocess.CompletedProcess(argv, 0, None, b"")

    d = desktop(Fakes(), run=run, env={"SystemRoot": r"C:\Windows", "PATH": "x"})
    apps = d._start_apps()
    assert [a.name for a in apps] == ["Hesap Makinesi", "Görev Yöneticisi"]
    assert seen["creationflags"] == 0x08000000  # no console flashes over the user's work
    # By full path under SystemRoot: a powershell.exe earlier on PATH is not run.
    exe = seen["argv"][0].replace("\\", "/")
    assert exe.startswith("C:/Windows/System32/WindowsPowerShell/") and exe.endswith(
        "powershell.exe"
    )
    assert "-NoProfile" in seen["argv"] and "Get-StartApps" in seen["argv"][-1]
    assert seen["env"]["PATH"] == "x"  # PowerShell still gets the real environment
    assert not Path(seen["env"]["JARVIS_STARTAPPS_OUT"]).exists()  # the temp file is gone


def test_a_failing_powershell_is_an_error_for_that_source_only(tmp_path: Path) -> None:
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, None, b"Get-StartApps : not recognized")

    menu = tmp_path / "Programs"
    menu.mkdir()
    (menu / "Notepad.lnk").write_text("")
    f = Fakes(folders={W.KNOWN_FOLDERS["programs"]: str(menu)})
    d = desktop(f, run=run, registry=FakeWinreg({}))
    names = {a.name for a in d.apps()}
    assert "Notepad" in names and "Bluetooth settings" in names
    assert "startapps" in d.catalog.errors and "not recognized" in d.catalog.errors["startapps"]


def test_launch_opens_only_what_the_catalog_listed(tmp_path: Path) -> None:
    menu = tmp_path / "Programs"
    menu.mkdir()
    (menu / "Notepad.lnk").write_text("")
    f = Fakes(folders={W.KNOWN_FOLDERS["programs"]: str(menu)})

    def no_powershell(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, None, b"")

    d = desktop(f, run=no_powershell, registry=FakeWinreg({}))
    notepad = next(a for a in d.apps() if a.name == "Notepad")
    d.launch(notepad)
    assert d.started == [str(menu / "Notepad.lnk")]  # type: ignore[attr-defined]
    for forged in (
        App("Notepad", r"C:\Windows\System32\calc.exe", "apppaths"),
        App("x", "ms-settings:troubleshoot", "settings"),
        App("x", "https://evil.example", "startapps"),
    ):
        with pytest.raises(PcRefused, match="only open apps I found"):
            d.launch(forged)
    assert len(d.started) == 1  # type: ignore[attr-defined]


def test_open_url_checks_again_before_the_shell_sees_it() -> None:
    d = desktop(Fakes())
    d.open_url("youtube.com")
    with pytest.raises(PcRefused):
        d.open_url("ms-msdt:/id x")
    assert d.started == ["https://youtube.com"]  # type: ignore[attr-defined]


def test_open_path_refuses_a_program_even_if_asked_directly(tmp_path: Path) -> None:
    d = desktop(Fakes(), env={"PATHEXT": ".COM;.EXE"})
    (tmp_path / "a.exe").write_text("MZ")
    (tmp_path / "a.pdf").write_text("pdf")
    with pytest.raises(PcRefused):
        d.open_path(tmp_path / "a.exe")
    d.open_path(tmp_path / "a.pdf")
    assert d.started == [str(tmp_path / "a.pdf")]  # type: ignore[attr-defined]


def test_building_the_desktop_loads_nothing_and_reads_nothing() -> None:
    # tool_extra() rebuilds it whenever settings change; it must cost nothing.
    d = W.WindowsDesktop(env={}, startfile=lambda t: None)
    assert d._api is None and not d.catalog.loaded
    assert d.status().available
