"""System-wide hotkeys for the editor.

Primary path: ``RegisterHotKey`` on a dedicated message-loop thread.  If a
combination is already owned by another program, a low-level keyboard hook is
installed as a fallback so the shortcut still works.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

_MODIFIER_VKS = {
    0x10, 0xA0, 0xA1,          # shift
    0x11, 0xA2, 0xA3,          # control
    0x12, 0xA4, 0xA5,          # alt
    0x5B, 0x5C,                # win
}

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                  wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN}

_NAMED = {
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "space": 0x20,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B,
}


def parse_hotkey(seq: str):
    parts = [p.strip() for p in seq.split("+") if p.strip()]
    if not parts:
        return None
    key = parts[-1].lower()
    mods = 0
    for part in parts[:-1]:
        bit = _MODS.get(part.lower())
        if bit is None:
            return None
        mods |= bit
    if mods == 0:
        # bare letters would swallow normal typing; only allow F-keys
        if not (key.startswith("f") and key[1:].isdigit()):
            return None
    vk = _NAMED.get(key)
    if vk is None:
        if len(key) == 1 and key.isalnum():
            vk = ord(key.upper())
        elif key.startswith("f") and key[1:].isdigit():
            n = int(key[1:])
            if 1 <= n <= 24:
                vk = 0x70 + n - 1
        if vk is None:
            return None
    return mods, vk


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t


class GlobalHotkeys:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._running = False
        self._queue: queue.Queue[str] = queue.Queue()
        self._actions: dict[int, str] = {}
        self._ready = threading.Event()
        self.errors: list[str] = []
        self._fallback: dict[int, tuple[str, int]] = {}
        self._hook = None
        self._hook_proc = None
        self._down: set[int] = set()
        self._pressed: set[int] = set()

    def start(self, mapping: dict[str, str]) -> list[str]:
        self.stop()
        parsed = []
        for action, seq in mapping.items():
            p = parse_hotkey(seq)
            if p:
                parsed.append((action, p[0], p[1]))
        self._actions.clear()
        self._fallback.clear()
        self.errors = []
        self._ready.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, args=(parsed,), name="global-hotkeys", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)
        return self.errors

    def stop(self) -> None:
        self._running = False
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None
        self._tid = 0
        self._unhook()

    def poll(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    # -- fallback hook ---------------------------------------------------
    @staticmethod
    def _phys(vks) -> bool:
        return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)

    def _mods_match(self, mods: int) -> bool:
        def down(vks) -> bool:
            return bool(set(vks) & self._pressed) or self._phys(vks)

        ctrl = down((0x11, 0xA2, 0xA3))
        alt = down((0x12, 0xA4, 0xA5))
        shift = down((0x10, 0xA0, 0xA1))
        win = down((0x5B, 0x5C))
        return (ctrl == bool(MOD_CONTROL & mods)
                and alt == bool(MOD_ALT & mods)
                and shift == bool(MOD_SHIFT & mods)
                and win == bool(MOD_WIN & mods))

    def _on_key(self, n_code, w_param, l_param):
        try:
            if n_code == 0:
                data = ctypes.cast(l_param,
                                   ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(data.vkCode)
                msg = int(w_param)
                if vk in _MODIFIER_VKS:
                    if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self._pressed.add(vk)
                    elif msg in (WM_KEYUP, WM_SYSKEYUP):
                        self._pressed.discard(vk)
                if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    entry = self._fallback.get(vk)
                    if entry and vk not in self._down:
                        action, mods = entry
                        if self._mods_match(mods):
                            self._down.add(vk)
                            self._queue.put(action)
                            self._log(f"hit vk=0x{vk:02x} action={action}")
                elif msg in (WM_KEYUP, WM_SYSKEYUP):
                    self._down.discard(vk)
        except Exception:  # noqa: BLE001
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _poll_fallback(self) -> None:
        """Poll physical keys for combinations RegisterHotKey could not take.

        A polling loop avoids hook/module-handle issues and still works while
        another program owns the same RegisterHotKey combination.
        """
        while self._running:
            for vk, (action, mods) in list(self._fallback.items()):
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if down and self._mods_match(mods):
                    if vk not in self._down:
                        self._down.add(vk)
                        self._queue.put(action)
                elif not down:
                    self._down.discard(vk)
            time.sleep(0.03)

    def _install_hook(self) -> None:
        self._hook_proc = HOOKPROC(self._on_key)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc,
            kernel32.GetModuleHandleW(None), 0,
        )

    def _unhook(self) -> None:
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._hook_proc = None

    # -- message loop ----------------------------------------------------
    def _log(self, message: str) -> None:
        try:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            path = Path(base) / "FixMath" / "logs" / "hotkeys.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
        except OSError:
            pass

    def _run(self, parsed) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        for action, mods, vk in parsed:
            self._fallback[vk] = (action, mods)

        hmod = kernel32.GetModuleHandleW(None)
        self._hook_proc = HOOKPROC(self._on_key)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL,
                                              self._hook_proc, hmod, 0)
        self._log(f"hook={bool(self._hook)} err={ctypes.get_last_error()} "
                  f"mapping={self._fallback}")
        if not self._hook:
            # No interactive desktop / hook denied: fall back to polling.
            self._ready.set()
            self._poll_fallback()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
        self._unhook()
