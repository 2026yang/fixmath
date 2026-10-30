# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FixMath (P0 skeleton).

Build from the repository root:
    py -3.11 -m pip install pyinstaller
    pyinstaller packaging/fixmath.spec --noconfirm

The model weights are bundled by build.ps1 into
dist\\FixMath\\models\\pix2text and read by recognize_server at runtime.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller

extra_datas = collect_data_files("matplotlib") + collect_data_files("pix2text")
extra_binaries = []
extra_hidden = []
for pkg in ("pymupdf", "rapidocr", "cnstd", "cnocr", "transformers",
            "optimum", "tokenizers", "onnxruntime", "pystray"):
    try:
        datas, binaries, hidden = collect_all(pkg)
    except Exception:  # noqa: BLE001
        continue
    extra_datas += datas
    extra_binaries += binaries
    extra_hidden += hidden

a = Analysis(
    [str(ROOT / "fixmath_app.py")],
    pathex=[
        str(ROOT),
        str(ROOT / "capture"),
        str(ROOT / "editor"),
        str(ROOT / "ocr"),
    ],
    binaries=extra_binaries,
    datas=[
        (str(ROOT / "ocr" / "recognize_server.py"), "ocr"),
        (str(ROOT / "capture" / "fixmath"), "capture/fixmath"),
        (str(ROOT / "editor" / "clipboard_image.py"), "editor"),
        (str(ROOT / "editor" / "clipboard_text.py"), "editor"),
        (str(ROOT / "editor" / "wps_style.py"), "editor"),
        (str(ROOT / "packaging" / "fixmath.ico"), "."),
        (str(ROOT / "vendor" / "latex2mathml"), "vendor/latex2mathml"),
    ] + extra_datas,
    hiddenimports=[
        "fixmath.app",
        "fixmath.locate",
        "fixmath.win32",
        "fixmath.logutil",
        "recognize_server",
        "fixmath_editor",
        "clipboard_image",
        "clipboard_text",
        "wps_style",
        "global_hotkeys",
        "pystray",
        "mathtype_bridge",
        "version",
        "update_checker",
        "matplotlib.backends.backend_agg",
        "pix2text.latex_ocr",
        "pix2text.consts",
        "onnxruntime",
    ] + extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter.test", "matplotlib.tests", "pytest",
        "tensorflow",
        "IPython", "notebook", "jupyter", "sqlalchemy",
        "torchvision", "torchaudio",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FixMath",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    version=str(ROOT / "packaging" / "version_info.txt"),
    icon=str(ROOT / "packaging" / "fixmath.ico") if (ROOT / "packaging" / "fixmath.ico").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="FixMath",
)
