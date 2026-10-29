"""FixMath capture helper (P0).

Press the global hotkey right after selecting a formula with the left mouse
button in *any* application.  The helper recovers the selection rectangle from
the screen pixels, crops it, removes the selection highlight, shows a preview
and writes the result to disk.

    python -m fixmath.app --hotkey ctrl+alt+m
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk

from .locate import (LocateResult, clean_crop, expand_rect, expand_to_ink,
                     locate, locate_hover, rect_area)
from .logutil import setup_logging
from .win32 import (InputService, click_at, cursor_pos, dpi_aware, dpi_scale,
                    foreground_is_browser, kernel32, process_integrity,
                    virtual_screen)

APP_NAME = "FixMath"
DEFAULT_HOTKEY = "ctrl+alt+7"
OCR_URL = "http://127.0.0.1:8765/ocr"


def try_ocr(image_path: Path, timeout: float = 30.0) -> dict:
    """Ask the local recognition service; capture must still work without it."""
    payload = json.dumps({"path": str(image_path)}).encode("utf-8")
    req = urllib.request.Request(
        OCR_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - offline is a normal state
        return {"error": str(exc)}


def fixes_path() -> Path:
    base = os.environ.get("FIXMATH_HOME") or os.environ.get("LOCALAPPDATA") \
        or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "fixes.json"


def apply_fixes(latex: str) -> str:
    """User-defined replacements, e.g. [["\\\\nu", "v"]]."""
    if not latex:
        return latex
    try:
        rules = json.loads(fixes_path().read_text(encoding="utf-8-sig")).get("replace", [])
    except (OSError, ValueError):
        return latex
    for pair in rules:
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
            latex = latex.replace(pair[0], str(pair[1]))
    return latex


def config_path() -> Path:
    base = os.environ.get("FIXMATH_HOME") or os.environ.get("LOCALAPPDATA") \
        or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "config.json"


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def prune_captures(root: Path, keep: int) -> None:
    """Delete the oldest capture folders, keeping the newest `keep`."""
    if keep <= 0:
        return
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for folder in folders[:-keep]:
        shutil.rmtree(folder, ignore_errors=True)


def resolve_hotkey(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        value = data.get("hotkey")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError):
        pass
    return DEFAULT_HOTKEY


# --------------------------------------------------------------------------- #
# capture pipeline
# --------------------------------------------------------------------------- #
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _shift_rect(r, dx, dy):
    return (r[0] - dx, r[1] - dy, r[2] - dx, r[3] - dy)


def capture_once(tracker, out_root: Path, preview_queue=None, ocr: bool = True) -> dict:
    t0 = time.perf_counter()
    dpi = dpi_scale()
    drag, click = tracker.snapshot()
    cur = cursor_pos()

    # A perfectly horizontal/vertical drag has zero area.  Inflate it instead
    # of discarding it: highlight detection supplies the missing extent.
    if drag is not None:
        w = drag[2] - drag[0]
        h = drag[3] - drag[1]
        if w < 4 and h < 4:
            drag = None
        elif h < 3:
            drag = (drag[0], drag[1] - 2, drag[2], drag[3] + 2)
        elif w < 3:
            drag = (drag[0] - 2, drag[1], drag[2] + 2, drag[3])

    if drag is None and click is None:
        click = cur

    # Mode is decided by the foreground window; the old ocr_profile config
    # key is ignored so a stale "web" value cannot break WPS captures.
    profile = "web" if foreground_is_browser() else "wps"

    if profile == "web" and click is not None:
        # Hover mode uses the cursor, not the last drag: a short drag would
        # clip a long formula at the ROI border.
        seed = (click[0] - 900, click[1] - 300, click[0] + 900, click[1] + 300)
    elif drag is not None:
        seed = drag
    else:
        seed = (click[0] - 500, click[1] - 160, click[0] + 500, click[1] + 160)

    vx, vy, vw, vh = virtual_screen()
    # Generous margin: a short drag must still leave the whole selection inside
    # the captured region, otherwise the highlight gets clipped at the border.
    margin = int(round(160 * dpi))
    roi = expand_rect(seed, margin, margin)
    roi = (
        _clamp(roi[0], vx, vx + vw - 2),
        _clamp(roi[1], vy, vy + vh - 2),
        _clamp(roi[2], vx + 2, vx + vw),
        _clamp(roi[3], vy + 2, vy + vh),
    )
    if profile == "web":
        # Web pages: grab the whole screen so a long formula can never be cut
        # by the ROI border; the ink locator narrows it afterwards.
        roi = (vx, vy, vx + vw, vy + vh)

    if profile == "web" and drag is not None:
        # Chrome ignores Esc for text selection; click the page margin just
        # left of the selection.  A margin click clears it without placing a
        # caret on the formula or following a link.
        margin_x = drag[0] - 40
        click_x = margin_x if margin_x > 10 else (drag[0] + drag[2]) // 2
        click_at(click_x, (drag[1] + drag[3]) // 2)
        time.sleep(0.40)

    full = ImageGrab.grab(all_screens=True)
    shot = full.crop((roi[0] - vx, roi[1] - vy, roi[2] - vx, roi[3] - vy))

    drag_local = _shift_rect(drag, roi[0], roi[1]) if drag else None
    click_local = (click[0] - roi[0], click[1] - roi[1]) if click else None

    if profile == "web" and drag_local is not None:
        # Selection was just collapsed.  Start from the dragged rectangle and
        # only complete it across small blank gaps; a large gap (next prose
        # line) stops the growth, so this stays the user's range.
        res = LocateResult(
            box=expand_to_ink(shot, drag_local, pad=max(4, int(4 * dpi))),
            mode="web",
        )
    elif profile == "web":
        # No drag: hover mode, locate the formula block under the cursor.
        target = click_local or (shot.width // 2, shot.height // 2)
        res = locate_hover(shot, target, dpi) or locate(
            shot, drag=drag_local, click=click_local, dpi_scale=dpi
        )
    else:
        res = locate(shot, drag=drag_local, click=click_local, dpi_scale=dpi)
    global_box = (res.box[0] + roi[0], res.box[1] + roi[1],
                  res.box[2] + roi[0], res.box[3] + roi[1])

    probe = np.asarray(shot.crop(res.box), dtype=np.float32)
    probe_lum = probe @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    blue_mask = (probe[..., 2] > probe[..., 0] + 50.0) & (probe[..., 2] > 140.0)
    white_on_blue = bool(
        blue_mask.mean() > 0.25
        and float(np.median(probe_lum[blue_mask])) < 170.0
    )

    clean = clean_crop(shot, res)
    crop = shot.crop(res.box)

    # 96 px was the minimum, but mfr-1.5 confuses italic v with \nu at that
    # scale; 144 px (1.5x) fixes it in our tests.
    upscale = 1.0
    target_height = 144
    if clean.height < target_height:
        upscale = min(3.0, target_height / max(1, clean.height))
        clean = clean.resize(
            (max(1, int(round(clean.width * upscale))),
             max(1, int(round(clean.height * upscale)))),
            Image.LANCZOS,
        )

    elapsed = (time.perf_counter() - t0) * 1000

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    out_dir = out_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    shot.save(out_dir / "shot.png")
    crop.save(out_dir / "crop.png")
    clean.save(out_dir / "clean.png")

    # Same image with the white background made transparent, for pasting into
    # Word/WPS as a picture (avoids their LaTeX parsers entirely).
    transparent = np.asarray(clean.convert("RGBA")).copy()
    white = (transparent[..., :3] >= 245).all(axis=2)
    transparent[..., 3] = np.where(white, 0, 255)
    Image.fromarray(transparent, "RGBA").save(out_dir / "transparent.png")

    debug = shot.copy()
    dd = ImageDraw.Draw(debug)
    if drag_local:
        dd.rectangle(drag_local, outline=(255, 190, 0), width=2)
    if res.hl_box:
        dd.rectangle(res.hl_box, outline=(0, 220, 220), width=2)
    dd.rectangle(res.box, outline=(0, 200, 0), width=2)
    dd.text((4, 2), f"{res.mode}  {res.box[2]-res.box[0]}x{res.box[3]-res.box[1]}", fill=(0, 160, 0))
    debug.save(out_dir / "debug.png")

    box_w = res.box[2] - res.box[0]
    box_h = res.box[3] - res.box[1]
    tiny = box_w < 60 or box_h < 25

    # Show the crop immediately; OCR can take a second or more and must not
    # delay the preview.
    if preview_queue is not None:
        preview_queue.put(("preview", clean, {
            "size": [box_w, box_h],
            "mode": res.mode,
            "ms": round(elapsed, 1),
            "score": 0.0,
            "unreliable": tiny,
        }))

    ocr_result = {"latex": "", "score": 0.0, "ms": 0.0}
    if ocr:
        ocr_result = try_ocr(out_dir / "clean.png")

    latex_raw = apply_fixes(ocr_result.get("latex", ""))
    latex = latex_raw
    score = float(ocr_result.get("score") or 0.0)
    review = ""

    unreliable = bool(ocr and not ocr_result.get("error")) and (
        tiny or not latex_raw or (0.0 < score < 0.85)
        or len(latex_raw) > 700 or latex_raw.count("{{}}") > 8
    )
    if white_on_blue and profile != "web":
        # Browser/DeepSeek selections render white glyphs on a saturated blue
        # band; automatic extraction breaks the strokes.  Do not emit LaTeX.
        review = "浏览器选区（白字蓝底），请用 Win+Shift+S + 从剪贴板识别"
        latex = ""
    elif ocr_result.get("skipped") == "text":
        review = "检测到文字段落，请手动框选公式"
        latex = ""
    elif unreliable:
        # Do not hand a garbage crop to the editor/history; ask for a manual box.
        review = "自动框选不可靠，请手动框选"
        latex = ""
    elif latex and re.search(r"\\(sum|prod|iint|iiint|int)(?![_^])", latex):
        review = "大型运算符缺少上下限"
    elif latex and 0.0 < score < 0.90:
        review = "OCR 置信度偏低，请核对"

    meta = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "mode": res.mode,
        "profile": profile,
        "roi": list(roi),
        "box_global": list(global_box),
        "box_local": list(res.box),
        "size": [res.box[2] - res.box[0], res.box[3] - res.box[1]],
        "clean_size": [clean.width, clean.height],
        "upscale": round(upscale, 2),
        "dpi_scale": dpi,
        "elapsed_ms": round(elapsed, 1),
        "latex": latex,
        "latex_raw": latex_raw,
        "white_on_blue": white_on_blue,
        "review": review,
        "score": ocr_result.get("score", 0.0),
        "ocr_ms": ocr_result.get("ms", 0.0),
        "ocr_error": ocr_result.get("error", ""),
        "files": {
            "shot": str(out_dir / "shot.png"),
            "crop": str(out_dir / "crop.png"),
            "clean": str(out_dir / "clean.png"),
            "transparent": str(out_dir / "transparent.png"),
            "debug": str(out_dir / "debug.png"),
        },
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_root / "latest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        (out_root / "latest.png").write_bytes((out_dir / "clean.png").read_bytes())
    except OSError:
        pass

    try:
        keep = int(load_config().get("keep_captures", 200))
    except (TypeError, ValueError):
        keep = 200
    prune_captures(out_root, keep)

    return meta


# --------------------------------------------------------------------------- #
# preview overlay (tkinter, never steals focus)
# --------------------------------------------------------------------------- #
class Preview:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.win: Optional[tk.Toplevel] = None
        self.imgtk = None
        self._job = None

    def close(self):
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self.imgtk = None

    def show(self, image: Image.Image, info: dict, ttl: int = 1500):
        self.close()
        w, h = image.size
        scale = 1.0
        if w > 860:
            scale = 860 / w
        if h * scale > 440:
            scale = min(scale, 440 / h)
        if scale < 1.0:
            image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#4caf50")

        frame = tk.Frame(win, bg="#4caf50")
        frame.pack(fill="both", expand=True, padx=2, pady=2)

        caption = f"{info['size'][0]}x{info['size'][1]}  {info['mode']}  {info['ms']:.0f} ms"
        if info.get("unreliable"):
            caption += "  · 框选不可靠，请手动框选"
        bar = tk.Label(frame, text=caption, bg="#202020", fg="#e8e8e8",
                       font=("Microsoft YaHei UI", 9), anchor="w", padx=6)
        bar.pack(fill="x")

        self.imgtk = ImageTk.PhotoImage(image)
        label = tk.Label(frame, image=self.imgtk, bg="#ffffff", bd=0)
        label.pack(fill="both", expand=True)

        win.update_idletasks()
        cw, ch = win.winfo_width(), win.winfo_height()
        sx, sy, sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight(), 0, 0
        cx, cy = cursor_pos()
        x = _clamp(cx + 18, 0, max(0, sx - cw - 8))
        y = _clamp(cy + 18, 0, max(0, sy - ch - 8))
        win.geometry(f"+{x}+{y}")
        win.deiconify()

        for seq in ("<Button-1>", "<Escape>"):
            win.bind(seq, lambda _e: self.close())
        self.win = win
        self._job = self.root.after(ttl, self.close)


# --------------------------------------------------------------------------- #
# application
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, args):
        self.args = args
        self.log, self.log_path = setup_logging()
        self.out_root = Path(args.out).expanduser()
        self.out_root.mkdir(parents=True, exist_ok=True)
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.preview = Preview(self.root)
        self.input = InputService(args.hotkey, max_age=args.max_age,
                                  debug=args.debug, logger=self.log)
        self.running = True
        self.worker = threading.Thread(target=self._work, name="capture-worker", daemon=True)

    def _watch_hotkey(self) -> None:
        """Apply config.json hotkey changes without restarting the helper."""
        current = self.input._hotkey
        while self.running:
            time.sleep(1.0)
            spec = resolve_hotkey(None)
            if spec and spec != current:
                self.input.update_hotkey(spec)
                self.log.info(f"[key] 热键已更新为 {spec.upper()}")
                current = spec

    def run(self) -> int:
        if not self.input.start():
            self.log.error(f"[FixMath] 启动失败: {self.input.error}")
            return 2
        self.log.info(f"[FixMath] 已启动，热键 {self.args.hotkey.upper()}（组合键已被拦截，不会传给前台程序）")
        self.log.info(f"[FixMath] 输出目录 {self.out_root}")
        self.log.info(f"[FixMath] 配置 {config_path()}（不存在则用默认热键）")
        self.log.info(f"[FixMath] 进程完整性级别: {process_integrity()}；DPI: {dpi_scale():.2f}")
        self.log.info(f"[FixMath] 日志文件 {self.log_path}")
        self.log.info("[FixMath] 在任意程序中用左键选中公式，然后按热键。Ctrl+C 退出。")
        self.worker.start()
        threading.Thread(target=self._watch_hotkey, name="hotkey-watch", daemon=True).start()
        self.root.after(40, self._poll)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.input.stop()
        return 0

    def _poll(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if item[0] == "preview":
                    self.preview.show(item[1], item[2])
                elif item[0] == "log":
                    self.log.info(item[1])
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _work(self):
        while self.running:
            try:
                evt = self.input.events.get(timeout=0.3)
            except queue.Empty:
                continue
            if evt != "hotkey":
                continue
            try:
                if self.args.debug:
                    self.log.info(f"[work] snapshot={self.input.tracker.snapshot()}")
                meta = capture_once(self.input.tracker, self.out_root, self.ui_queue,
                                    ocr=not self.args.no_ocr)
                self.log.info(f"[FixMath] {meta['mode']} {meta['size']} {meta['elapsed_ms']}ms -> {meta['files']['crop']}")
                if meta.get("latex"):
                    self.log.info(f"[ocr] {meta['score']:.3f}  {meta['latex']}")
                    if meta.get("review"):
                        self.log.info(f"[review] {meta['review']}")
                elif meta.get("ocr_error"):
                    self.log.info(f"[ocr] 未识别（服务未启动？）: {meta['ocr_error']}")
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"[FixMath] 截取失败: {exc}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="FixMath capture helper")
    ap.add_argument("--hotkey", default=None,
                    help="e.g. ctrl+alt+q, ctrl+shift+alt+m, ctrl+alt+f9")
    ap.add_argument("--out", default=str(Path.home() / "AppData/Local/FixMath/captures"))
    ap.add_argument("--max-age", type=float, default=120.0,
                    help="how long a mouse selection stays valid (seconds)")
    ap.add_argument("--once", action="store_true",
                    help="capture once around the cursor and exit (smoke test)")
    ap.add_argument("--debug", action="store_true", help="log the first raw mouse events")
    ap.add_argument("--no-ocr", action="store_true", help="capture only, do not call the OCR service")
    args = ap.parse_args(argv)
    args.hotkey = resolve_hotkey(args.hotkey)

    dpi_aware()

    if args.once:
        class _Empty:
            def snapshot(self):
                return None, None

        meta = capture_once(_Empty(), Path(args.out).expanduser(), ocr=not args.no_ocr)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    # Single instance: a second copy would install a second hook and make the
    # hotkey behaviour depend on install order.
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.LPCWSTR]
    mutex = kernel32.CreateMutexW(None, 0, "Local\\FixMathCaptureHelper")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("[FixMath] 已经有一个助手在运行了。请先关掉它（控制台 Ctrl+C 或任务管理器结束 python.exe），再启动新的。")
        return 0

    return App(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
