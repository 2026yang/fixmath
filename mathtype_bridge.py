"""Copy a LaTeX formula as a real MathType equation.

MathType 6/7 does not expose a documented "LaTeX -> clipboard" API from
Python, but its editor has a TeX toggle: paste the LaTeX source, press
Alt+\\ (Toggle TeX), select all, copy.  The clipboard then carries the
native MathType equation (MTEF), which WPS + the MathType add-in paste as an
editable equation object.

This module automates that sequence; it requires MathType to be installed.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9
SW_MINIMIZE = 6
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56
VK_DELETE = 0x2E
VK_OEM_5 = 0xDC  # backslash
VK_HOME = 0x24
VK_END = 0x23
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_LEFT = 0x25

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                 wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


def find_mathtype() -> Path | None:
    candidates = [
        Path(r"C:\Program Files (x86)\MathType\MathType.exe"),
        Path(r"C:\Program Files\MathType\MathType.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _set_clipboard_text(text: str) -> None:
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _find_math_type_window(timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if "mathtype" in title.lower():
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def _key(vk: int, down: bool) -> None:
    user32.keybd_event(vk, 0, 0 if down else 2, 0)


def _combo(modifier: int, vk: int) -> None:
    _key(modifier, True)
    _key(vk, True)
    _key(vk, False)
    _key(modifier, False)
    time.sleep(0.2)


def _tap(vk: int) -> None:
    _key(vk, True)
    _key(vk, False)
    time.sleep(0.2)


def _clipboard_formats() -> list[str]:
    names: list[str] = []
    if not user32.OpenClipboard(None):
        return names
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            buf = ctypes.create_unicode_buffer(256)
            if user32.GetClipboardFormatNameW(fmt, buf, 256):
                names.append(buf.value)
    finally:
        user32.CloseClipboard()
    return names


def latex_to_mathml(latex: str) -> str | None:
    """Convert LaTeX to MathML using the vendored latex2mathml package."""
    import sys

    vendor = Path(__file__).resolve().parent / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    try:
        from latex2mathml.converter import convert

        return convert(latex)
    except Exception:  # noqa: BLE001
        return None


def _wrap_cases_fence(mathml: str) -> str:
    """Wrap an mtable in <mfenced open="{"> so the brace spans the rows.

    latex2mathml emits a plain <mo>{</mo> before <mtable>; MathType imports
    the table but does not stretch that fence.  mfenced is the construct
    MathType's MathML importer understands as a multi-row brace.
    """
    import xml.etree.ElementTree as ET

    ns = "http://www.w3.org/1998/Math/MathML"
    ET.register_namespace("", ns)
    try:
        root = ET.fromstring(mathml)
    except ET.ParseError:
        return mathml

    for parent in list(root.iter()):
        children = list(parent)
        for index, child in enumerate(children):
            if child.tag != f"{{{ns}}}mo":
                continue
            if (child.text or "").strip() not in ("{", "\u007b"):
                continue
            table = None
            for follower in children[index + 1:]:
                if follower.tag == f"{{{ns}}}mtable":
                    table = follower
                    break
            if table is None:
                continue
            parent.remove(child)
            parent.remove(table)
            table.set("columnalign", "left")
            table.set("columnspacing", "0em")
            table.set("rowspacing", "0.15em")
            fenced = ET.Element(
                f"{{{ns}}}mfenced",
                {"open": "{", "close": "", "separators": ""},
            )
            fenced.append(table)
            parent.insert(index, fenced)
            break
    return ET.tostring(root, encoding="unicode")


def needs_mathml(latex: str) -> bool:
    """Cases/matrices need structured MathML; aligned rows work line-by-line."""
    return bool(re.search(
        r"\\begin\{(cases|matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|array|smallmatrix)\}",
        latex,
    ))


def mathml_for_mathtype(latex: str) -> str | None:
    mathml = latex_to_mathml(latex)
    if not mathml:
        return None
    if "mtable" in mathml:
        return _wrap_cases_fence(mathml)
    return mathml


def _has_mathtype_clipboard() -> bool:
    for name in _clipboard_formats():
        lower = name.lower()
        if "mathtype" in lower or "mtef" in lower:
            return True
    return False


def normalize_for_mathtype(latex: str) -> str:
    """Make OCR LaTeX acceptable to MathType's single-line TeX parser.

    MathType's TeX input understands neither ``aligned`` nor ``\\matrix``;
    feeding those produces literal red ``&`` / ``\\end{aligned}`` text.  The
    only reliable TeX-level form is one line, so drop the display wrapper,
    alignment tabs and row separators and join the rows with ``\\quad``.
    """
    text = latex.strip()
    for env in (
        "aligned", "alignedat", "gathered", "split",
        "align", "align*", "equation", "equation*",
        "gather", "gather*", "multline", "multline*",
        "displaymath", "math",
    ):
        text = re.sub(rf"\\begin\{{{re.escape(env)}\}}(\[[^\]]*\])?", " ", text)
        text = re.sub(rf"\\end\{{{re.escape(env)}\}}", " ", text)
    text = text.replace("&", " ")
    text = re.sub(r"\\\\", r" \\quad ", text)
    text = re.sub(r"\\(nonumber|notag|label\{[^}]*\}|tag\{[^}]*\})", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(\\quad\s*)+$", "", text).strip()
    return text


def _display_rows(latex: str) -> list[str]:
    """Split a display environment into per-line TeX strings.

    ``&`` cells are concatenated (the alignment point becomes a space) and a
    continuation row that starts with ``&`` is indented with ``\\quad``.
    """
    match = re.search(
        r"\\begin\{(aligned|alignedat|align\*?|gathered|gather\*?|split|eqnarray\*?)\}"
        r"(?:\[[^\]]*\])?(.*?)\\end\{\1\}",
        latex,
        re.S,
    )
    if not match:
        return [normalize_for_mathtype(latex)]
    rows: list[str] = []
    for raw in re.split(r"\\\\", match.group(2)):
        if not raw.strip():
            continue
        raw_cells = [cell.strip() for cell in raw.split("&")]
        first = raw_cells[0] if raw_cells else ""
        continued = first in ("", "{}", "{{}}")
        cells = [cell for cell in raw_cells
                 if cell and cell not in ("{}", "{{}}")]
        if not cells:
            continue
        text = " ".join(cells)
        if continued:
            text = r"\quad " + text
        rows.append(text)
    return rows or [normalize_for_mathtype(latex)]


def _select_left(count: int) -> None:
    """Select exactly the last ``count`` characters (the pasted TeX row)."""
    _key(VK_SHIFT, True)
    for _ in range(max(1, min(count, 400))):
        _key(VK_LEFT, True)
        _key(VK_LEFT, False)
    _key(VK_SHIFT, False)
    time.sleep(0.1)


def copy_latex_as_mathtype(latex: str, exe: Path | None = None,
                           timeout: float = 25.0,
                           background: bool = False) -> tuple[bool, str]:
    """Return (ok, message).  On success the clipboard holds a MathType
    equation ready to paste into WPS.

    ``background`` is used for global shortcuts: after MathType has produced
    the equation, minimize it and return focus to the window the user was
    working in, so the shortcut does not leave them inside MathType.
    """
    previous = user32.GetForegroundWindow()
    exe = exe or find_mathtype()
    if exe is None:
        return False, "未找到 MathType.exe"

    hwnd = _find_math_type_window(timeout=1.0)
    if hwnd is None:
        subprocess.Popen([str(exe)])
        hwnd = _find_math_type_window(timeout=timeout)
        if hwnd is None:
            return False, "MathType 启动超时"

    def restore_user_window():
        if not background:
            return
        try:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            if previous and previous != hwnd:
                # Only un-minimize. SW_RESTORE on a maximized window would
                # shrink it back to its restored size.
                if user32.IsIconic(previous):
                    user32.ShowWindow(previous, SW_RESTORE)
                user32.SetForegroundWindow(previous)
        except Exception:  # noqa: BLE001
            pass

    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    rows = _display_rows(latex)
    mathml = mathml_for_mathtype(latex) if needs_mathml(latex) else None
    if mathml:
        _set_clipboard_text(mathml)
        time.sleep(0.15)
        _combo(VK_CONTROL, VK_A)
        _tap(VK_DELETE)
        _combo(VK_CONTROL, VK_V)
        time.sleep(1.0)
        _combo(VK_CONTROL, VK_A)
        _combo(VK_CONTROL, VK_C)
        time.sleep(0.4)
        if _has_mathtype_clipboard():
            restore_user_window()
            return True, "已复制 MathType 公式（MathML），到 WPS 里 Ctrl+V"
        _combo(VK_CONTROL, VK_A)
        _tap(VK_DELETE)

    _combo(VK_CONTROL, VK_A)
    _tap(VK_DELETE)
    for index, row in enumerate(rows):
        _set_clipboard_text(row)
        time.sleep(0.15)
        _combo(VK_CONTROL, VK_V)
        time.sleep(0.35)
        # Select just the pasted TeX and convert only that line.
        _select_left(len(row))
        _key(VK_MENU, True)
        _tap(VK_OEM_5)
        _key(VK_MENU, False)
        time.sleep(0.45)
        _tap(VK_END)
        if index < len(rows) - 1:
            _tap(VK_RETURN)
    _combo(VK_CONTROL, VK_A)
    _combo(VK_CONTROL, VK_C)
    time.sleep(0.4)

    if _has_mathtype_clipboard():
        restore_user_window()
        return True, "已复制 MathType 公式，到 WPS 里 Ctrl+V"
    restore_user_window()
    return False, "MathType 未生成公式，请确认窗口已打开并重试"
