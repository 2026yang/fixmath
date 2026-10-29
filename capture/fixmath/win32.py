"""Thin Win32 layer: DPI awareness, global mouse hook, hotkey, cursor.

Everything here is ctypes-only so the capture helper has no third-party
dependency beyond Pillow/numpy (both bundled offline).
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- constants
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

MODIFIER_VKS = {
    0x10, 0xA0, 0xA1,          # shift
    0x11, 0xA2, 0xA3,          # control
    0x12, 0xA4, 0xA5,          # alt
    0x5B, 0x5C,                # win
}

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

LRESULT = ctypes.c_ssize_t
HHOOK = ctypes.c_void_p


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# ---------------------------------------------------------------- basics
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def capture_paused() -> bool:
    """True while the editor's shortcut dialog is capturing keys."""
    try:
        base = os.environ.get("FIXMATH_HOME") or os.environ.get("LOCALAPPDATA") \
            or str(Path.home() / "AppData" / "Local")
        return (Path(base) / "FixMath" / "pause_capture").exists()
    except OSError:
        return False


def click_at(x: int, y: int) -> None:
    """Click once at (x, y) and put the cursor back.

    Chrome does not clear a text selection on Esc; a click on the text does.
    Clicking a formula is safe (no navigation) and collapses the selection.
    """
    old = cursor_pos()
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.SetCursorPos(old[0], old[1])


def foreground_is_browser() -> bool:
    """True when a known browser process owns the foreground window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    name = ""
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)

    browsers = {
        "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
        "vivaldi.exe", "iexplore.exe", "360se.exe", "360chrome.exe",
        "qqbrowser.exe", "sogouexplorer.exe", "maxthon.exe", "theworld.exe",
        "liebao.exe", "2345explorer.exe",
    }
    if name in browsers:
        return True

    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value.lower()
    return (
        "chrome_widgetwin" in cls
        or "mozilla" in cls
        or "applicationframewindow" in cls
    )


def dpi_aware() -> None:
    try:
        # PER_MONITOR_AWARE_V2
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale() -> float:
    try:
        return float(user32.GetDpiForSystem()) / 96.0
    except Exception:
        return 1.0


def virtual_screen() -> Tuple[int, int, int, int]:
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return vx, vy, max(1, vw), max(1, vh)


def cursor_pos() -> Tuple[int, int]:
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def process_integrity() -> str:
    """Return 'low' / 'medium' / 'high' for the current process token.

    Global low-level hooks do not receive input from higher-integrity
    processes, so this is the first thing to check when nothing fires.
    """
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.HANDLE)]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
        advapi.GetTokenInformation.restype = wintypes.BOOL
        advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                       ctypes.byref(token)):
            return "unknown"

        size = wintypes.DWORD(0)
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
        if size.value == 0:
            return "unknown"
        buf = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 25, buf, size, ctypes.byref(size)):
            return "unknown"

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", SID_AND_ATTRIBUTES)]

        label = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        cnt = advapi.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        rid = advapi.GetSidSubAuthority(label.Label.Sid, cnt - 1).contents.value
        return {0x1000: "low", 0x2000: "medium", 0x3000: "high"}.get(rid, hex(rid))
    except Exception:
        return "unknown"


def _dist(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _norm(a: Tuple[int, int], b: Tuple[int, int]) -> Tuple[int, int, int, int]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


# ---------------------------------------------------------------- tracker
class SelectionTracker:
    """Remembers the most recent left-drag (and click) in *any* application."""

    def __init__(self, max_age: float = 120.0, min_drag: float = 4.0):
        self._lock = threading.Lock()
        self._down: Optional[Tuple[int, int]] = None
        self._dragging = False
        self._last: Optional[Tuple[int, int]] = None
        self._drag = None
        self._drag_at = 0.0
        self._click = None
        self._click_at = 0.0
        self.max_age = max_age
        self.min_drag = min_drag

    def on_mouse(self, msg: int, pt: Tuple[int, int]) -> None:
        with self._lock:
            if msg == WM_LBUTTONDOWN:
                self._down = pt
                self._dragging = True
                self._last = pt
            elif msg == WM_MOUSEMOVE and self._dragging:
                self._last = pt
            elif msg == WM_LBUTTONUP:
                self._dragging = False
                if self._down is not None and _dist(self._down, pt) > self.min_drag:
                    self._drag = _norm(self._down, pt)
                    self._drag_at = time.time()
                else:
                    self._click = pt
                    self._click_at = time.time()
                self._down = None

    def snapshot(self):
        with self._lock:
            now = time.time()
            drag = None
            click = None
            if self._drag and now - self._drag_at <= self.max_age:
                drag = self._drag
            if self._click and now - self._click_at <= self.max_age:
                click = self._click
            if self._dragging and self._down is not None:
                drag = _norm(self._down, self._last or self._down)
            return drag, click


# ---------------------------------------------------------------- input thread
class InputService:
    """Owns the low-level mouse hook and the global hotkey.

    The hook callback only records coordinates.  Pressing the hotkey drops the
    string "hotkey" into ``events``; the worker thread performs the capture so
    the hook never exceeds the LowLevelHooksTimeout budget.
    """

    def __init__(self, hotkey: str = "ctrl+alt+m", max_age: float = 120.0,
                 debug: bool = False, logger=None):
        self.events: "queue.Queue[str]" = queue.Queue()
        self.tracker = SelectionTracker(max_age=max_age)
        self._mods, self._vk = parse_hotkey(hotkey)
        self._vks = {self._vk}
        if 0x30 <= self._vk <= 0x39:          # digits: top row and numpad both work
            self._vks.add(self._vk + 0x40)
        self._hotkey = hotkey
        self.debug = debug
        self.logger = logger
        self._last_mismatch = 0.0
        self.event_count = 0
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None
        self._hook = None
        self._proc = None
        self._kb_hook = None
        self._kb_proc = None
        self._key_down = False
        self._pressed: set[int] = set()
        self._hotkey_registered = False
        self._ready = threading.Event()
        self.error: Optional[str] = None

    # -- public ------------------------------------------------------------
    def update_hotkey(self, spec: str) -> None:
        """Hot-reload the hotkey (called when config.json changes)."""
        try:
            mods, vk = parse_hotkey(spec)
        except Exception:  # noqa: BLE001
            return
        self._mods, self._vk = mods, vk
        self._vks = {vk}
        if 0x30 <= vk <= 0x39:
            self._vks.add(vk + 0x40)
        self._hotkey = spec
        self._log(f"hotkey updated: {spec}")

    def start(self, timeout: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="input-hook", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.error is None

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.5)

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message)
        elif self.debug:
            print(message, flush=True)

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        try:
            hmod = kernel32.GetModuleHandleW(None)

            self._proc = HOOKPROC(self._on_mouse)
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0)
            if not self._hook:
                raise ctypes.WinError(ctypes.get_last_error())

            # Keyboard hook: the combo is swallowed before the foreground app
            # sees it, so applications with the same local shortcut (WPS uses
            # Ctrl+Alt+M) never react.
            self._kb_proc = HOOKPROC(self._on_key)
            self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
            if not self._kb_hook:
                self._hotkey_registered = bool(
                    user32.RegisterHotKey(None, 1, self._mods, self._vk)
                )
                if not self._hotkey_registered:
                    raise RuntimeError(
                        f"无法安装键盘钩子，且全局热键 {self._hotkey} 注册失败"
                    )

            self._tid = kernel32.GetCurrentThreadId()
            self._ready.set()
            self._log(
                f"[hook] 已安装 mouse={bool(self._hook)} keyboard={bool(self._kb_hook)}"
                f" hotkey={self._hotkey}"
            )

            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == WM_HOTKEY:
                    if not capture_paused():
                        self.events.put("hotkey")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            self.error = str(exc)
            self._ready.set()
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
            if self._kb_hook:
                user32.UnhookWindowsHookEx(self._kb_hook)
                self._kb_hook = None
            try:
                user32.UnregisterHotKey(None, 1)
            except Exception:
                pass

    @staticmethod
    def _phys(vks) -> bool:
        return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)

    def _mods_ok(self) -> bool:
        # Use physical key state instead of the hook's own set: if a modifier
        # was pressed before the hook started, or another hook swallowed its
        # key-down, the tracked set misses it and the hotkey never matches.
        ctrl = self._phys((0x11, 0xA2, 0xA3))
        alt = self._phys((0x12, 0xA4, 0xA5))
        shift = self._phys((0x10, 0xA0, 0xA1))
        win = self._phys((0x5B, 0x5C))
        if (MOD_CONTROL & self._mods) and not ctrl:
            return False
        if (MOD_ALT & self._mods) and not alt:
            return False
        if (MOD_SHIFT & self._mods) and not shift:
            return False
        if (MOD_WIN & self._mods) and not win:
            return False
        return (ctrl == bool(MOD_CONTROL & self._mods)
                and alt == bool(MOD_ALT & self._mods)
                and shift == bool(MOD_SHIFT & self._mods)
                and win == bool(MOD_WIN & self._mods))

    def _on_key(self, n_code, w_param, l_param):
        if n_code == 0:
            try:
                data = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                msg = int(w_param)
                vk = int(data.vkCode)

                if vk in MODIFIER_VKS:
                    if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self._pressed.add(vk)
                    elif msg in (WM_KEYUP, WM_SYSKEYUP):
                        self._pressed.discard(vk)

                if vk in self._vks:
                    if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if capture_paused():
                            return user32.CallNextHookEx(
                                None, n_code, w_param, l_param
                            )
                        if self._mods_ok():
                            if not self._key_down:
                                self._key_down = True
                                self._log(f"[key] 热键命中 {self._hotkey}")
                                self.events.put("hotkey")
                            return 1          # swallow + block auto-repeat
                        self._key_down = False
                        now = time.time()
                        if now - self._last_mismatch > 1.5:
                            self._last_mismatch = now
                            mods = sorted(hex(v) for v in self._pressed
                                          if v in MODIFIER_VKS)
                            self._log(
                                f"[key] 按到了 vk=0x{vk:02x}，但修饰键是 {mods}，"
                                f"与 {self._hotkey} 不匹配"
                            )
                    elif msg in (WM_KEYUP, WM_SYSKEYUP) and self._key_down:
                        self._key_down = False
                        return 1              # swallow the matching key-up
            except Exception:
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_mouse(self, n_code, w_param, l_param):
        if n_code == 0:
            try:
                data = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                self.event_count += 1
                if self.debug and self.event_count <= 10:
                    self._log(f"[mouse] msg={int(w_param)} pt=({data.pt.x},{data.pt.y})")
                self.tracker.on_mouse(int(w_param), (int(data.pt.x), int(data.pt.y)))
                if self.debug and int(w_param) == WM_LBUTTONUP:
                    self._log(f"[mouse] up -> drag={self.tracker._drag} click={self.tracker._click}")
            except Exception:
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)


def parse_hotkey(spec: str) -> Tuple[int, int]:
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods = 0
    key = None
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "meta"):
            mods |= MOD_WIN
        else:
            key = p
    if not key:
        raise ValueError("hotkey has no key (e.g. ctrl+alt+q)")
    if len(key) == 1:
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unsupported hotkey key: {key!r}")
    return mods | MOD_NOREPEAT, vk
