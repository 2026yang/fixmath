"""FixMath single-entry launcher.

One process owns the whole app:
  * single-instance mutex (double-clicking again just focuses the running one)
  * starts the OCR service if it is not already healthy
  * starts the capture helper and the editor
  * when the editor closes, stops the helper and the OCR service it started

This is the entry point a packaged build (PyInstaller) should use.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "FixMath"
BASE = Path(__file__).resolve().parent
OCR_URL = "http://127.0.0.1:8765"
MUTEX_NAME = "Global\\FixMath.Launcher"
ERROR_ALREADY_EXISTS = 183
CREATE_NEW_PROCESS_GROUP = 0x00000200

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def data_dir() -> Path:
    base = os.environ.get("FIXMATH_HOME") or os.environ.get("LOCALAPPDATA") \
        or str(Path.home() / "AppData" / "Local")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("fixmath")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    handler = None
    try:
        logs = data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            logs / "launcher.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        import tempfile

        try:
            fallback = Path(tempfile.gettempdir()) / APP_NAME / "logs"
            fallback.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                fallback / "launcher.log", maxBytes=2_000_000, backupCount=3,
                encoding="utf-8",
            )
        except OSError:
            handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def message(text: str, title: str = APP_NAME, flags: int = 0x40) -> None:
    user32.MessageBoxW(None, text, title, flags)


def ocr_healthy(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{OCR_URL}/health", timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def wait_for_ocr(proc: subprocess.Popen, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ocr_healthy():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def terminate(proc: subprocess.Popen | None, name: str, logger: logging.Logger) -> None:
    if proc is None or proc.poll() is not None:
        return
    logger.info("stopping %s (pid %s)", name, proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=6)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def acquire_single_instance():
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def run_role(role: str) -> int:
    """Internal entry points used by the launcher (and by frozen builds)."""
    saved = sys.argv
    sys.argv = [saved[0]]
    try:
        if role == "ocr":
            sys.path.insert(0, str(BASE / "ocr"))
            import recognize_server

            return recognize_server.main()
        if role == "capture":
            sys.path.insert(0, str(BASE / "capture"))
            from fixmath.app import main as capture_main

            return capture_main()
        if role == "editor":
            sys.path.insert(0, str(BASE / "editor"))
            import fixmath_editor

            return fixmath_editor.main()
    finally:
        sys.argv = saved
    return 2


def spawn(role: str) -> subprocess.Popen:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--role", role]
    else:
        cmd = [sys.executable, str(BASE / "fixmath_app.py"), "--role", role]
    return subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "FixMath"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{BASE / "fixmath_app.py"}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            return str(value) == autostart_command()
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def focus_window(pid: int) -> None:
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

    def callback(hwnd, _lparam):
        proc_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)


def start_tray(procs: dict, quit_event: threading.Event, logger):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None

    image = Image.new("RGB", (64, 64), (26, 115, 232))
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 14, 50, 50), outline="white", width=4)
    draw.text((27, 22), "F", fill="white")

    def show_editor(*_args):
        proc = procs.get("editor")
        if proc and proc.poll() is None:
            focus_window(proc.pid)
            return
        procs["editor"] = spawn("editor")

    def toggle_capture(*_args):
        proc = procs.get("capture")
        if proc and proc.poll() is None:
            terminate(proc, "capture helper", logger)
            procs["capture"] = None
        else:
            procs["capture"] = spawn("capture")

    def toggle_autostart(icon, _item):
        set_autostart(not autostart_enabled())
        icon.update_menu()

    def quit_app(icon, _item):
        quit_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("显示编辑器", show_editor, default=True),
        pystray.MenuItem("暂停/恢复截图", toggle_capture),
        pystray.MenuItem("开机自启", toggle_autostart,
                         checked=lambda _item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("FixMath", image, "FixMath", menu)
    return icon


def run_control_window(procs: dict, logger) -> None:
    """Small always-available control window (used instead of a tray icon).

    pystray's blocking loop does not survive the frozen windowed build here,
    so the launcher keeps a normal window in the taskbar; closing it minimizes
    instead of exiting.
    """
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("FixMath")
    root.geometry("340x170")
    root.resizable(False, False)

    ttk.Label(root, text="FixMath 正在运行", font=("Microsoft YaHei UI", 11, "bold")).pack(
        pady=(14, 2)
    )
    status = ttk.Label(root, text="截图热键已生效，可最小化此窗口", foreground="#666")
    status.pack(pady=(0, 10))

    def show_editor():
        proc = procs.get("editor")
        if proc and proc.poll() is None:
            focus_window(proc.pid)
        else:
            procs["editor"] = spawn("editor")

    def toggle_capture():
        proc = procs.get("capture")
        if proc and proc.poll() is None:
            terminate(proc, "capture helper", logger)
            procs["capture"] = None
            status.configure(text="截图已暂停")
        else:
            procs["capture"] = spawn("capture")
            status.configure(text="截图已恢复")

    row = ttk.Frame(root)
    row.pack(pady=4)
    ttk.Button(row, text="显示编辑器", command=show_editor).pack(side="left", padx=4)
    ttk.Button(row, text="暂停/恢复截图", command=toggle_capture).pack(side="left", padx=4)

    auto = tk.BooleanVar(value=autostart_enabled())

    def toggle_autostart():
        set_autostart(bool(auto.get()))

    ttk.Checkbutton(root, text="开机自启", variable=auto,
                    command=toggle_autostart).pack(pady=(6, 0))
    ttk.Button(root, text="退出 FixMath", command=root.destroy).pack(pady=(8, 10))

    root.protocol("WM_DELETE_WINDOW", root.iconify)
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["ocr", "capture", "editor"])
    parser.add_argument("--health", action="store_true", help="print OCR health and exit")
    parser.add_argument("--no-editor", action="store_true", help="start services only")
    parser.add_argument("--no-tray", action="store_true", help="run without tray icon")
    args = parser.parse_args()

    if args.role:
        return run_role(args.role)

    logger = setup_logging()

    if args.health:
        healthy = ocr_healthy()
        if sys.stdout is not None:
            print("ocr healthy" if healthy else "ocr down")
        return 0 if healthy else 1

    mutex = acquire_single_instance()
    if mutex is None:
        message("FixMath 已经在运行。\n请从任务栏切换到已打开的窗口。", "FixMath")
        return 0

    procs: dict = {"ocr": None, "capture": None, "editor": None}
    started_ocr = False
    quit_event = threading.Event()
    icon = None

    try:
        if not ocr_healthy():
            logger.info("starting OCR service")
            procs["ocr"] = spawn("ocr")
            started_ocr = True
            if not wait_for_ocr(procs["ocr"]):
                logger.error("OCR service failed to start")
                message(
                    "OCR 服务启动失败。\n请查看日志：\n"
                    f"{data_dir() / 'logs' / 'launcher.log'}",
                    "FixMath",
                    0x10,
                )
                return 2
        else:
            logger.info("using existing OCR service")

        logger.info("starting capture helper")
        procs["capture"] = spawn("capture")

        logger.info("starting editor")
        procs["editor"] = spawn("editor")

        def show_editor():
            proc = procs.get("editor")
            if proc and proc.poll() is None:
                focus_window(proc.pid)
            else:
                procs["editor"] = spawn("editor")

        def toggle_capture():
            proc = procs.get("capture")
            if proc and proc.poll() is None:
                terminate(proc, "capture helper", logger)
                procs["capture"] = None
            else:
                procs["capture"] = spawn("capture")

        def toggle_autostart():
            set_autostart(not autostart_enabled())

        def capture_alive() -> bool:
            proc = procs.get("capture")
            return bool(proc and proc.poll() is None)

        if not args.no_tray:
            try:
                from tray_win32 import TrayIcon

                tray = TrayIcon(
                    BASE / "fixmath.ico",
                    show_editor,
                    toggle_capture,
                    toggle_autostart,
                    autostart_enabled,
                    capture_alive,
                )
                logger.info("native tray started")
                tray.run()
                return 0
            except Exception:  # noqa: BLE001
                logger.exception("native tray failed; falling back to control window")

        logger.info("control window started")
        run_control_window(procs, logger)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass
        terminate(procs["capture"], "capture helper", logger)
        if started_ocr:
            terminate(procs["ocr"], "OCR service", logger)
        if mutex:
            kernel32.CloseHandle(mutex)
        logger.info("launcher exit")


if __name__ == "__main__":
    raise SystemExit(main())
