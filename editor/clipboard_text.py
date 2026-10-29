"""Read a formula directly from the clipboard (text / HTML / MathML).

Chat pages render formulas with KaTeX/MathJax, which usually embed the
original TeX in ``<annotation encoding="application/x-tex">``.  Copying the
formula and parsing that annotation gives exact LaTeX without OCR.
"""

from __future__ import annotations

import ctypes
import html
import re
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def set_text(text: str) -> None:
    """Put plain Unicode text on the clipboard without Tk ownership issues."""
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _read_bytes(fmt: int) -> bytes:
    if not user32.OpenClipboard(None):
        return b""
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return b""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return b""
        try:
            size = kernel32.GlobalSize(handle)
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def get_text() -> str:
    raw = _read_bytes(CF_UNICODETEXT)
    if not raw:
        return ""
    try:
        return raw.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return ""


def get_html() -> str:
    fmt = user32.RegisterClipboardFormatW("HTML Format")
    raw = _read_bytes(fmt)
    return raw.decode("utf-8", "ignore") if raw else ""


_TEX_ANNOTATION = re.compile(
    r'<annotation[^>]*encoding=["\']application/x-tex["\'][^>]*>(.*?)</annotation>',
    re.S | re.I,
)

_TEXT_PATTERNS = (
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\\\[(.+?)\\\]", re.S),
    re.compile(r"\\\((.+?)\\\)", re.S),
    re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.S),
)


def extract_latex() -> str:
    raw_html = get_html()
    if raw_html:
        match = _TEX_ANNOTATION.search(raw_html)
        if match:
            tex = html.unescape(match.group(1)).strip()
            if tex:
                return tex

    text = get_text().strip()
    if not text:
        return ""
    for pattern in _TEXT_PATTERNS:
        match = pattern.search(text)
        if match:
            tex = match.group(1).strip()
            if tex:
                return tex
    if "\\begin{" in text and "\\end{" in text:
        return text
    return ""


if __name__ == "__main__":
    print("text:", repr(get_text()[:200]))
    print("latex:", repr(extract_latex()))
