"""FixMath 公式编辑器 MVP（全离线）。

左侧显示截图，右侧是可编辑的 LaTeX；带上下限缺失提示、历史翻页和一键复制。
监听 captures/latest.json，新截图到达时自动跳到最新一条。

    py -3.11 fixmath_editor.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageGrab, ImageTk

try:  # optional modern theme; the app still runs on plain ttk
    import ttkbootstrap as ttkb
except Exception:  # noqa: BLE001
    ttkb = None

try:
    from global_hotkeys import GlobalHotkeys
except Exception:  # noqa: BLE001
    GlobalHotkeys = None

APP_NAME = "FixMath"
try:
    from version import __version__ as APP_VERSION
except Exception:  # noqa: BLE001
    APP_VERSION = "0.1.0"
FONT = "Microsoft YaHei UI"
OCR_URL = "http://127.0.0.1:8765/ocr"


def default_captures() -> Path:
    base = os.environ.get("FIXMATH_HOME") or os.environ.get("LOCALAPPDATA") \
        or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "captures"


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


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def make_button(parent, text, command, primary=False, **kwargs):
    if ttkb is not None:
        return ttkb.Button(parent, text=text, command=command,
                           bootstyle=("primary" if primary else "secondary"),
                           **kwargs)
    return ttk.Button(parent, text=text, command=command, **kwargs)


class Editor:
    def __init__(self, root: tk.Tk, folder: Path, watch: bool = True):
        self.root = root
        self.folder = folder
        self.watch = watch
        self.entries: list[Path] = []
        self.index = -1
        self.meta: dict = {}
        self.dirty = False
        self._latest_mtime = 0.0
        self._ocr_token = None
        self._ocr_queue: queue.Queue = queue.Queue()
        self._preview_job = None
        self.preview_image = None
        self._tree_guard = False
        self.global_hotkeys = GlobalHotkeys() if GlobalHotkeys is not None else None
        self._bound_sequences: list[str] = []
        cfg = load_config()
        shortcuts = cfg.get("shortcuts", {})
        self.shortcuts: dict[str, str] = shortcuts if isinstance(shortcuts, dict) else {}
        self.photo = None

        root.title("FixMath 公式编辑器")
        root.geometry("1180x680")
        root.minsize(900, 520)

        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        self._build_ui_v2()
        self.reload(select_last=True)
        self._apply_shortcuts()
        self.root.bind_all("<KeyPress>", self._on_key, add="+")
        self.root.after(100, self._poll_ocr)
        self.root.after(150, self._poll_global_hotkeys)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    # ------------------------------------------------------------- modern UI
    def _build_ui_v2(self):
        root = self.root
        root.title("FixMath · 离线公式识别")
        try:
            self.ui_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self.ui_scale = 1.0
        root.geometry(f"{int(1280 * self.ui_scale)}x{int(820 * self.ui_scale)}")
        root.minsize(int(1100 * self.ui_scale), int(700 * self.ui_scale))

        style = ttk.Style(root)
        try:
            style.configure("TButton", font=(FONT, 9), padding=(10, 4))
            style.configure("Treeview", font=(FONT, 9), rowheight=26)
            style.configure("Treeview.Heading", font=(FONT, 9, "bold"))
            style.configure("TLabelframe.Label", font=(FONT, 9, "bold"))
        except tk.TclError:
            pass

        header = ttk.Frame(root, padding=(16, 10, 16, 6))
        header.grid(row=0, column=0, sticky="ew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        ttk.Label(header, text="FixMath", font=(FONT, 15, "bold")).pack(side="left")
        ttk.Label(header, text="离线公式识别", foreground="#80868b",
                  font=(FONT, 9)).pack(side="left", padx=(10, 0))
        self.counter = ttk.Label(header, text="0/0", foreground="#5f6368",
                                 font=(FONT, 10))
        self.counter.pack(side="right")

        self.status = ttk.Label(root, text="等待截图…", anchor="w",
                                padding=(14, 6), relief="sunken")
        self.status.grid(row=4, column=0, sticky="ew")

        self.review = tk.Label(root, text="", bg="#fff4e5", fg="#8a4b00",
                               anchor="w", padx=14, pady=6, font=(FONT, 9))

        body = ttk.Frame(root, padding=(12, 6, 12, 4))
        body.grid(row=2, column=0, sticky="nsew")
        self.body = body
        body.columnconfigure(0, weight=0, minsize=230)
        body.columnconfigure(1, weight=3)
        body.columnconfigure(2, weight=4)
        body.rowconfigure(0, weight=1)

        # ---- history sidebar
        side = ttk.Frame(body)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ttk.Label(side, text="历史记录", font=(FONT, 10, "bold")).pack(anchor="w")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._fill_tree())
        ttk.Entry(side, textvariable=self.search_var).pack(fill="x", pady=(6, 6))
        tree_wrap = ttk.Frame(side)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=("time", "score"),
                                 show="headings", selectmode="browse")
        self.tree.heading("time", text="时间", anchor="w")
        self.tree.heading("score", text="分", anchor="center")
        self.tree.column("time", width=128, anchor="w", stretch=True)
        self.tree.column("score", width=50, anchor="center", stretch=False)
        tsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ---- center: cleaned / boxed image cards
        center = ttk.Frame(body)
        center.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        center.rowconfigure(0, weight=1)
        center.rowconfigure(1, weight=1)
        center.columnconfigure(0, weight=1)

        self.raw_card = tk.Frame(center, bg="#ffffff", highlightthickness=2,
                                 highlightbackground="#e8eaed")
        self.raw_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.raw_title = tk.Label(self.raw_card, text="清洗图（clean.png）",
                                  bg="#ffffff", fg="#5f6368",
                                  font=(FONT, 9, "bold"), anchor="w")
        self.raw_title.pack(fill="x", padx=12, pady=(8, 0))
        self.raw_hl = tk.Frame(self.raw_card, bg="#ffffff")
        self.raw_hl.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        self.raw_label = tk.Label(self.raw_hl, bg="#ffffff", text="（还没有截图）",
                                  fg="#9aa0a6", font=(FONT, 10), cursor="hand2")
        self.raw_label.pack(fill="both", expand=True)
        for widget in (self.raw_label, self.raw_hl, self.raw_card):
            widget.bind("<Button-1>", lambda _e: self._ocr_image("raw"))

        self.box_card = tk.Frame(center, bg="#ffffff", highlightthickness=2,
                                 highlightbackground="#e8eaed")
        self.box_card.grid(row=1, column=0, sticky="nsew")
        self.box_title = tk.Label(self.box_card, text="框选图（手动框选生成）",
                                  bg="#ffffff", fg="#5f6368",
                                  font=(FONT, 9, "bold"), anchor="w")
        self.box_title.pack(fill="x", padx=12, pady=(8, 0))
        self.box_hl = tk.Frame(self.box_card, bg="#ffffff")
        self.box_hl.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        self.box_label = tk.Label(self.box_hl, bg="#ffffff", text="（还没有框选图）",
                                  fg="#9aa0a6", font=(FONT, 10), cursor="hand2")
        self.box_label.pack(fill="both", expand=True)
        for widget in (self.box_label, self.box_hl, self.box_card):
            widget.bind("<Button-1>", lambda _e: self._ocr_image("box"))

        # ---- right: editor + preview
        right = ttk.Frame(body)
        right.grid(row=0, column=2, sticky="nsew")
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        edit_card = ttk.Labelframe(right, text="LaTeX（可直接修改）", padding=8)
        edit_card.grid(row=0, column=0, sticky="nsew")
        edit_card.rowconfigure(0, weight=1)
        edit_card.columnconfigure(0, weight=1)
        self.text = tk.Text(edit_card, wrap="word", undo=True, width=1, height=8,
                            font=("Consolas", 11), relief="flat", bd=0,
                            padx=10, pady=10, highlightthickness=1,
                            highlightbackground="#e8eaed")
        text_sb = ttk.Scrollbar(edit_card, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=text_sb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        text_sb.grid(row=0, column=1, sticky="ns")
        self.text.bind("<<Modified>>", self._on_modified)

        prev_card = ttk.Labelframe(right, text="预览", padding=8)
        prev_card.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        prev_card.rowconfigure(0, weight=1)
        prev_card.columnconfigure(0, weight=1)
        self.preview_label = tk.Label(prev_card, bg="#ffffff", text="（预览）",
                                      fg="#9aa0a6", font=(FONT, 9))
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        # ---- action bar
        actions = ttk.Frame(root, padding=(12, 4, 12, 8))
        actions.grid(row=3, column=0, sticky="ew")
        row1 = ttk.Frame(actions)
        row1.pack(fill="x")
        make_button(row1, "复制 $…$", self.copy_inline, primary=True).pack(side="left")
        make_button(row1, "复制纯 LaTeX", self.copy_plain).pack(side="left", padx=6)
        make_button(row1, "复制原透明图", self.copy_raw_image).pack(side="left", padx=6)
        make_button(row1, "复制框选透明图", self.copy_box_image).pack(side="left", padx=6)
        make_button(row1, "复制为 MathType 公式", self.copy_mathtype,
                    primary=True).pack(side="left", padx=6)
        make_button(row1, "保存", self.save).pack(side="left", padx=6)

        row2 = ttk.Frame(actions)
        row2.pack(fill="x", pady=(6, 0))
        make_button(row2, "从剪贴板识别", self.ocr_clipboard).pack(side="left")
        make_button(row2, "手动框选", self.manual_crop).pack(side="left", padx=6)
        make_button(row2, "ν→v", self.fix_nu).pack(side="left", padx=6)
        make_button(row2, "快捷键", self.open_shortcuts).pack(side="left", padx=6)
        make_button(row2, "设置", self.open_keep_settings).pack(side="left", padx=6)
        make_button(row2, "打开目录", self.open_folder).pack(side="right")
        make_button(row2, "下一条", lambda: self.step(1)).pack(side="right", padx=6)
        make_button(row2, "上一条", lambda: self.step(-1)).pack(side="right")

        self.raw_pane = self.raw_card
        self.box_pane = self.box_card

        # ------------------------------------------------------------- history
    def _fill_tree(self):
        if not hasattr(self, "tree"):
            return
        self._tree_guard = True
        try:
            self.tree.delete(*self.tree.get_children())
            query = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
            for i, folder in enumerate(self.entries):
                score = ""
                try:
                    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8-sig"))
                    score = f"{float(meta.get('score') or 0):.2f}"
                except (OSError, ValueError):
                    pass
                if query and query not in folder.name.lower() and query not in score:
                    continue
                stamp = folder.name
                label = f"{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
                self.tree.insert("", "end", iid=str(i), values=(label, score))
            if 0 <= self.index < len(self.entries):
                iid = str(self.index)
                if self.tree.exists(iid):
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
        finally:
            self._tree_guard = False

    def _on_tree_select(self, _event=None):
        if getattr(self, "_tree_guard", False):
            return
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.entries) and idx != self.index:
            self.save(silent=True)
            self.index = idx
            self.load_current()

    def reload(self, select_last: bool = False):
        old_name = self.entries[self.index].name if 0 <= self.index < len(self.entries) else None
        items = []
        if self.folder.is_dir():
            for d in self.folder.iterdir():
                if d.is_dir() and (d / "meta.json").is_file():
                    items.append(d)
        items.sort(key=lambda p: p.name)
        self.entries = items

        if not items:
            self.index = -1
            self.status.configure(text="还没有截图，先在任意程序里选中公式按 Ctrl+Alt+7")
            self.counter.configure(text="0/0")
            return

        if select_last:
            self.index = len(items) - 1
        elif old_name:
            names = [p.name for p in items]
            self.index = names.index(old_name) if old_name in names else len(items) - 1
        else:
            self.index = len(items) - 1
        self.load_current()
        self._fill_tree()

    def step(self, delta: int):
        if not self.entries:
            return
        new = max(0, min(len(self.entries) - 1, self.index + delta))
        if new == self.index:
            return
        self.save(silent=True)
        self.index = new
        self.load_current()

    def load_current(self):
        if not (0 <= self.index < len(self.entries)):
            return
        folder = self.entries[self.index]
        try:
            self.meta = json.loads((folder / "meta.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            self.meta = {}

        edit_file = folder / "edit.json"
        latex = self.meta.get("latex", "")
        if edit_file.is_file():
            try:
                latex = json.loads(edit_file.read_text(encoding="utf-8-sig")).get("latex", latex)
            except (OSError, ValueError):
                pass

        self.text.delete("1.0", "end")
        self.text.insert("1.0", latex)
        self.text.edit_modified(False)
        self.text.edit_reset()
        self.dirty = False

        if not latex.strip() and self.meta.get("ocr_error"):
            self.status.configure(text=f"OCR 未返回结果：{self.meta['ocr_error']}")
        else:
            mode = self.meta.get("mode", "?")
            size = self.meta.get("size", [0, 0])
            score = self.meta.get("score", 0.0)
            self.status.configure(
                text=f"{folder.name}   ·   {mode}   ·   {size[0]}×{size[1]}   ·   OCR {score:.3f}"
            )
        self.counter.configure(text=f"{self.index + 1}/{len(self.entries)}")
        if hasattr(self, "tree"):
            self._fill_tree()

        review = self.meta.get("review", "")
        if review:
            self.review.configure(text=f"⚠ {review} —— 请对照左图补全后再复制")
            self.review.grid(row=1, column=0, sticky="ew")
        else:
            self.review.grid_remove()

        self._update_preview()

        self._show_image(folder)

    def _show_image(self, folder: Path):
        top = folder / "clean.png"
        if not top.is_file():
            top = folder / "crop.png"
        self._set_image(self.raw_label, top, "raw")
        manual = folder / "manual.png"
        if manual.is_file():
            self._set_image(self.box_label, manual, "box")
        else:
            self.box_label.configure(image="", text="（还没有框选图）")
        self._set_selected("raw" if top.is_file() else "")

    def _set_image(self, label: tk.Label, path: Path, attr: str):
        if path.is_file():
            try:
                im = Image.open(path).convert("RGB")
                im.thumbnail(
                    (int(420 * getattr(self, "ui_scale", 1.0)),
                     int(220 * getattr(self, "ui_scale", 1.0))),
                    Image.LANCZOS,
                )
                photo = ImageTk.PhotoImage(im)
                setattr(self, f"photo_{attr}", photo)
                label.configure(image=photo, text="")
                return
            except OSError:
                pass
        label.configure(image="", text="（没有图片）")

    # -------------------------------------------------------------- actions
    def _on_modified(self, _event=None):
        if self.text.edit_modified():
            self.dirty = True
            self.text.edit_modified(False)
            if self._preview_job:
                try:
                    self.root.after_cancel(self._preview_job)
                except tk.TclError:
                    pass
            self._preview_job = self.root.after(400, self._update_preview)

    def _update_preview(self):
        latex = self.current_text()
        if not latex:
            self.preview_label.configure(image="", text="（预览）")
            return
        if len(latex) > 500 or "\\begin{" in latex:
            self.preview_label.configure(
                image="", text="（结构较复杂，暂不预览；可到 WPS 中查看）", height=2
            )
            return
        if not hasattr(self, "_preview_cache"):
            self._preview_cache = {}
            self._preview_lock = threading.Lock()
        cached = self._preview_cache.get(latex)
        if cached is not None:
            self._show_preview_image(latex, cached)
            return
        token = object()
        self._preview_token = token
        self.preview_label.configure(image="", text="（正在渲染预览…）")
        threading.Thread(
            target=self._preview_worker, args=(latex, token), daemon=True
        ).start()

    def _preview_worker(self, latex: str, token: object):
        try:
            import matplotlib

            matplotlib.use("Agg")
            from matplotlib import mathtext

            buf = io.BytesIO()
            with self._preview_lock:
                mathtext.math_to_image("$" + latex + "$", buf, dpi=110,
                                       format="png")
            buf.seek(0)
            im = Image.open(buf).convert("RGB")
            im.thumbnail((520, 150), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            if token is getattr(self, "_preview_token", None):
                self.root.after(0, self._preview_unsupported)
            return
        if token is not getattr(self, "_preview_token", None):
            return
        self.root.after(0, lambda: self._show_preview_image(latex, im))

    def _show_preview_image(self, latex: str, im):
        try:
            photo = ImageTk.PhotoImage(im)
            self.preview_image = photo
            self.preview_label.configure(image=photo, text="", height=0)
            self._preview_cache[latex] = im
            while len(self._preview_cache) > 16:
                self._preview_cache.pop(next(iter(self._preview_cache)))
        except Exception:  # noqa: BLE001
            self.preview_label.configure(image="", text="（预览失败）")

    def _preview_unsupported(self):
        self.preview_label.configure(
            image="", text="（该结构暂不支持预览，可到 WPS/LaTeX 中查看）", height=2
        )

    def current_text(self) -> str:
        return self.text.get("1.0", "end").strip()

    def save(self, silent: bool = False):
        if not (0 <= self.index < len(self.entries)):
            return
        folder = self.entries[self.index]
        text = self.current_text()
        ocr_text = (self.meta.get("latex", "") or "").strip()

        # Only write edit.json when the text really differs from OCR; otherwise
        # "opened the entry" would look like a correction in the eval report.
        if text == ocr_text:
            try:
                (folder / "edit.json").unlink(missing_ok=True)
            except OSError:
                pass
            self.dirty = False
            if not silent:
                self.status.configure(text="与 OCR 一致，未生成 edit.json")
            return

        payload = {
            "latex": text,
            "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            (folder / "edit.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.dirty = False
            if not silent:
                self.status.configure(text=f"已保存 · {folder.name}")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def fix_nu(self):
        text = self.current_text()
        if not text:
            return
        new = text.replace("\\nu", "v")
        if new != text:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", new)
            self.dirty = True
            self.save(silent=True)
        self.status.configure(text="仅修改当前条目；没有写入全局规则")

    def ocr_clipboard(self):
        # grabclipboard() and the OCR request can both block; run them in a
        # worker so the window never freezes.
        self.status.configure(text="正在读取剪贴板…")
        token = object()
        self._ocr_token = token
        threading.Thread(target=self._clipboard_worker, args=(token,), daemon=True).start()

    def _clipboard_worker(self, token: object):
        try:
            img = ImageGrab.grabclipboard()
            if isinstance(img, list):
                loaded = None
                for path in img:
                    if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")):
                        loaded = Image.open(path)
                        break
                img = loaded
            if not isinstance(img, Image.Image):
                raise ValueError("剪贴板里没有图片。\n先用 Win+Shift+S 框住公式，再点这个按钮。")
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG")
            png = buf.getvalue()
            req = urllib.request.Request(
                OCR_URL, data=png,
                headers={"Content-Type": "image/png"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            data["_png"] = png
        except Exception as exc:  # noqa: BLE001
            self._ocr_queue.put(("clipboard", token, None, exc))
            return
        self._ocr_queue.put(("clipboard", token, data, None))

    def manual_crop(self):
        if not (0 <= self.index < len(self.entries)):
            return
        shot_path = self.entries[self.index] / "shot.png"
        if not shot_path.is_file():
            messagebox.showinfo("手动框选", "这个条目没有 shot.png", parent=self.root)
            return
        shot = Image.open(shot_path).convert("RGB")

        # Hide the editor while the user drags the selection; otherwise the
        # main window can pop over the canvas and steal the drag.
        self.root.withdraw()
        win = tk.Toplevel(self.root)
        win.title("手动框选：滚轮缩放，右键拖动平移，左键拉框识别（Esc 取消）")
        win.attributes("-topmost", True)

        def restore_editor():
            try:
                self.root.deiconify()
            except tk.TclError:
                pass

        keep_hidden = {"value": False}

        def close_win():
            try:
                win.destroy()
            except tk.TclError:
                pass
            if not keep_hidden["value"]:
                restore_editor()

        win.protocol("WM_DELETE_WINDOW", close_win)
        win.bind("<Destroy>", lambda event: restore_editor()
                 if event.widget is win and not keep_hidden["value"] else None)

        view_w, view_h = 980, 620
        scale = min(view_w / shot.width, view_h / shot.height, 1.0)

        frame = tk.Frame(win, bg="#222")
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, width=view_w, height=view_h,
                           cursor="cross", highlightthickness=0, bg="#222")
        xbar = tk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
        ybar = tk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        pending = {"ix": 0.0, "iy": 0.0, "x": 0, "y": 0, "active": False}
        jobs = {"fast": None, "crisp": None}

        def render(resample=Image.LANCZOS):
            disp = shot.resize(
                (max(1, int(shot.width * scale)), max(1, int(shot.height * scale))),
                resample,
            )
            photo = ImageTk.PhotoImage(disp)
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas.photo = photo
            canvas.config(scrollregion=(0, 0, disp.width, disp.height))
            if pending["active"]:
                bbox = canvas.bbox("all") or (0, 0, 1, 1)
                canvas.xview_moveto(max(0.0, min(1.0,
                    (pending["ix"] * scale - pending["x"]) / max(1, bbox[2]))))
                canvas.yview_moveto(max(0.0, min(1.0,
                    (pending["iy"] * scale - pending["y"]) / max(1, bbox[3]))))

        def schedule_render():
            for key in ("fast", "crisp"):
                if jobs[key]:
                    try:
                        win.after_cancel(jobs[key])
                    except tk.TclError:
                        pass
            jobs["fast"] = win.after(8, lambda: render(Image.BILINEAR))
            jobs["crisp"] = win.after(180, lambda: render(Image.LANCZOS))

        render()
        tk.Label(win, text="滚轮缩放 · 右键拖动平移 · 左键拉框识别 · Esc 取消",
                 bg="#202020", fg="#eee").pack(fill="x")

        state = {"x0": 0, "y0": 0, "rect": None}

        def on_down(event):
            state["x0"] = canvas.canvasx(event.x)
            state["y0"] = canvas.canvasy(event.y)
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                state["x0"], state["y0"], state["x0"], state["y0"],
                outline="#ff5252", width=2,
            )

        def on_move(event):
            if state["rect"]:
                canvas.coords(state["rect"], state["x0"], state["y0"],
                              canvas.canvasx(event.x), canvas.canvasy(event.y))

        def on_up(event):
            x1 = canvas.canvasx(event.x)
            y1 = canvas.canvasy(event.y)
            x0, x1 = sorted((state["x0"], x1))
            y0, y1 = sorted((state["y0"], y1))
            if x1 - x0 < 8 or y1 - y0 < 8:
                return
            rect = (
                int(x0 / scale), int(y0 / scale),
                int(x1 / scale), int(y1 / scale),
            )
            keep_hidden["value"] = True
            win.destroy()
            self._ocr_region(shot, rect)

        def on_wheel(event):
            nonlocal scale
            factor = 1.2 if event.delta > 0 else 1 / 1.2
            pending["ix"] = canvas.canvasx(event.x) / scale
            pending["iy"] = canvas.canvasy(event.y) / scale
            pending["x"], pending["y"] = event.x, event.y
            pending["active"] = True
            scale = max(0.15, min(4.0, scale * factor))
            schedule_render()

        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_up)
        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<ButtonPress-3>", lambda e: canvas.scan_mark(e.x, e.y))
        canvas.bind("<B3-Motion>", lambda e: canvas.scan_dragto(e.x, e.y, gain=1))
        win.bind("<Escape>", lambda _e: close_win())

    def _set_selected(self, which: str):
        on, off = "#1a73e8", "#e8eaed"
        self.raw_pane.configure(highlightbackground=on if which == "raw" else off)
        self.box_pane.configure(highlightbackground=on if which == "box" else off)
        self.raw_title.configure(fg="#1a73e8" if which == "raw" else "#5f6368")
        self.box_title.configure(fg="#1a73e8" if which == "box" else "#5f6368")

    def _ocr_image(self, which: str):
        if not (0 <= self.index < len(self.entries)):
            return
        path = self.entries[self.index] / ("clean.png" if which == "raw" else "manual.png")
        if not path.is_file():
            self.status.configure(text="还没有可识别的图片")
            return
        self._set_selected(which)
        self.status.configure(text="正在识别…")
        token = object()
        self._ocr_token = token
        threading.Thread(
            target=self._ocr_worker, args=(which, path, token), daemon=True
        ).start()

    def _ocr_worker(self, which: str, path: Path, token: object):
        try:
            data = self._recognize(path, which)
        except Exception as exc:  # noqa: BLE001
            self._ocr_queue.put((which, token, None, exc))
            return
        self._ocr_queue.put((which, token, data, None))

    def _poll_ocr(self):
        try:
            while True:
                which, token, data, error = self._ocr_queue.get_nowait()
                if getattr(self, "_ocr_token", None) is not token:
                    continue
                if error is not None:
                    if isinstance(error, ValueError):
                        messagebox.showinfo("从剪贴板识别", str(error), parent=self.root)
                    else:
                        messagebox.showerror(
                            "识别失败", f"{type(error).__name__}: {error}", parent=self.root
                        )
                    self.status.configure(text="识别失败")
                    continue
                self._apply_ocr(which, token, data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ocr)

    def _recognize(self, path: Path, which: str) -> dict:
        img = Image.open(path).convert("RGB")
        if which == "box":
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capture"))
            from fixmath.locate import LocateResult, clean_crop

            img = clean_crop(img, LocateResult(box=(0, 0, img.width, img.height), mode="manual"))
        if img.height < 144:
            s = 144 / img.height
            img = img.resize((int(img.width * s), 144), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        req = urllib.request.Request(
            OCR_URL, data=buf.getvalue(), headers={"Content-Type": "image/png"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _apply_ocr(self, which: str, token: object, data: dict):
        if getattr(self, "_ocr_token", None) is not token:
            return
        if which == "clipboard":
            self._create_clipboard_entry(data)
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", data.get("latex", ""))
        self.dirty = True
        self.save(silent=True)
        if which in ("raw", "box"):
            self._set_selected(which)
        label = {"raw": "清洗图", "box": "框选图", "clipboard": "剪贴板"}.get(which, which)
        self.status.configure(
            text=f"{label}识别 · {data.get('score', 0):.3f}"
        )

    def _create_clipboard_entry(self, data: dict):
        png = data.get("_png")
        if not png:
            self.status.configure(text="剪贴板识别失败：没有图片")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        folder = self.folder / stamp
        try:
            folder.mkdir(parents=True, exist_ok=True)
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img.save(folder / "shot.png")
            img.save(folder / "crop.png")

            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capture"))
            from fixmath.locate import LocateResult, clean_crop

            clean = clean_crop(
                img, LocateResult(box=(0, 0, img.width, img.height), mode="manual")
            )
            if clean.height < 144:
                s = 144 / clean.height
                clean = clean.resize(
                    (int(clean.width * s), 144), Image.LANCZOS
                )
            clean.save(folder / "clean.png")
            alpha = clean.convert("L").point(lambda v: 0 if v >= 245 else 255)
            rgba = clean.convert("RGBA")
            rgba.putalpha(alpha)
            rgba.save(folder / "transparent.png")

            meta = {
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "clipboard",
                "size": [img.width, img.height],
                "clean_size": [clean.width, clean.height],
                "latex": data.get("latex", ""),
                "score": float(data.get("score", 0.0) or 0.0),
                "review": "",
                "files": {
                    "shot": str(folder / "shot.png"),
                    "crop": str(folder / "crop.png"),
                    "clean": str(folder / "clean.png"),
                    "transparent": str(folder / "transparent.png"),
                },
            }
            (folder / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("剪贴板识别", f"保存失败：{exc}", parent=self.root)
            return

        self.reload(select_last=True)
        try:
            self.index = [p.name for p in self.entries].index(folder.name)
            self.load_current()
        except ValueError:
            pass
        self.status.configure(
            text=f"剪贴板识别 · {float(data.get('score', 0)):.3f}"
        )

    def _ocr_region(self, shot: Image.Image, rect):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "capture"))
        try:
            from fixmath.locate import LocateResult, clean_crop

            res = LocateResult(box=rect, mode="manual")
            clean = clean_crop(shot, res)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("裁剪失败", str(exc), parent=self.root)
            return
        if clean.height < 144:
            s = 144 / clean.height
            clean = clean.resize((int(clean.width * s), int(clean.height * s)), Image.LANCZOS)

        if 0 <= self.index < len(self.entries):
            manual_path = self.entries[self.index] / "manual.png"
            try:
                clean.save(manual_path)
                self._set_image(self.box_label, manual_path, "box")
                self._set_selected("box")
            except OSError:
                pass

        buf = io.BytesIO()
        clean.save(buf, "PNG")
        req = urllib.request.Request(
            OCR_URL, data=buf.getvalue(), headers={"Content-Type": "image/png"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("识别失败", f"OCR 服务没响应：{exc}", parent=self.root)
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", data.get("latex", ""))
        self.dirty = True
        self.save(silent=True)
        self.status.configure(text=f"手动框选识别 · {data.get('score', 0):.3f}")

    def to_wps(self):
        try:
            from wps_style import to_wps_style
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from wps_style import to_wps_style

        text = self.current_text()
        if not text:
            return
        new = to_wps_style(text)
        if new != text:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", new)
            self.dirty = True
            self.save(silent=True)
        self.status.configure(text="已转换为 WPS 公式格式")

    def copy_inline(self):
        text = self.current_text()
        self._log_editor(f"copy_inline text_len={len(text)}")
        if not text:
            self.status.configure(text="没有可复制的公式")
            return
        value = text if text.startswith("$") else f"${text}$"
        self._copy(value)
        self.status.configure(text="已复制 $…$")

    def copy_plain(self):
        text = self.current_text()
        if text:
            self._copy(text)
            self.status.configure(text="已复制纯 LaTeX")

    def _copy(self, value: str):
        try:
            from clipboard_text import get_text, set_text

            for attempt in range(3):
                set_text(value)
                time.sleep(0.03)
                current = get_text()
                self._log_editor(
                    f"clipboard set len={len(value)} verify={len(current)} try={attempt + 1}"
                )
                if current == value:
                    break
        except Exception as exc:  # noqa: BLE001
            self._log_editor(f"clipboard failed: {exc}")
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
        self.root.update_idletasks()
        preview = value[:36] + ("…" if len(value) > 36 else "")
        self._log_editor(f"clipboard value={preview!r}")
        self.status.configure(text=f"已复制：{value[:80]}{'…' if len(value) > 80 else ''}")

    def _copy_png_bytes(self, data: bytes, what: str):
        try:
            from clipboard_image import copy_png
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from clipboard_image import copy_png

        try:
            copy_png(data)
        except OSError as exc:
            messagebox.showerror(what, f"剪贴板写入失败：{exc}", parent=self.root)
            return
        self.status.configure(text=f"已复制{what}，可直接 Ctrl+V")

    def _transparent_png(self, path: Path) -> bytes:
        img = Image.open(path).convert("RGBA")
        pixels = [
            (r, g, b, 0) if r >= 245 and g >= 245 and b >= 245 else (r, g, b, a)
            for r, g, b, a in img.getdata()
        ]
        img.putdata(pixels)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def copy_mathtype(self, background: bool = False):
        latex = self.current_text().strip()
        if not latex:
            messagebox.showinfo("MathType", "当前没有 LaTeX", parent=self.root)
            return
        self.status.configure(text="正在调用 MathType 生成公式…")
        threading.Thread(target=self._mathtype_worker,
                         args=(latex, background), daemon=True).start()

    def copy_mathtype_shortcut(self):
        self.copy_mathtype(background=True)

    def _mathtype_worker(self, latex: str, background: bool = False):
        try:
            from mathtype_bridge import copy_latex_as_mathtype

            ok, msg = copy_latex_as_mathtype(latex, background=background)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"MathType 调用失败：{exc}"
        self.root.after(0, lambda: self.status.configure(text=msg))

    def copy_raw_image(self):
        if not (0 <= self.index < len(self.entries)):
            return
        folder = self.entries[self.index]
        transparent = folder / "transparent.png"
        if transparent.is_file():
            self._copy_png_bytes(transparent.read_bytes(), "原透明图（已去高亮）")
            return
        path = folder / "clean.png"
        if not path.is_file():
            messagebox.showinfo("复制原透明图", "这条记录没有 clean.png", parent=self.root)
            return
        self._copy_png_bytes(self._transparent_png(path), "原透明图（已去高亮）")

    def copy_box_image(self):
        if not (0 <= self.index < len(self.entries)):
            return
        path = self.entries[self.index] / "manual.png"
        if not path.is_file():
            messagebox.showinfo(
                "复制框选透明图",
                "这条记录还没有框选图。\n先点「手动框选」框一下公式。",
                parent=self.root,
            )
            return
        self._copy_png_bytes(self._transparent_png(path), "框选透明图")

    def _shortcut_actions(self):
        # Only the screenshot hotkey is global.  Everything else is a normal
        # button in the editor; no extra system-wide shortcuts are registered.
        return [
            ("capture", "截图（左键选中后按）", lambda: None),
        ]

    @staticmethod
    def _to_tk(seq: str) -> str:
        parts = [p.strip() for p in seq.split("+") if p.strip()]
        if not parts:
            return ""
        if parts[-1].lower() in ("ctrl", "control", "alt", "shift", "win", "meta"):
            return ""
        key = parts[-1]
        mods = []
        for part in parts[:-1]:
            low = part.lower()
            if low in ("ctrl", "control"):
                mods.append("Control")
            elif low == "shift":
                mods.append("Shift")
            elif low in ("alt", "option"):
                mods.append("Alt")
            elif low in ("win", "meta"):
                mods.append("Super")
        special = {
            "esc": "Escape", "space": "space", "left": "Left", "right": "Right",
            "up": "Up", "down": "Down", "del": "Delete", "delete": "Delete",
            "enter": "Return", "tab": "Tab",
        }
        key = special.get(key.lower(), key)
        return "<" + "-".join(mods + [key]) + ">"

    def _apply_shortcuts(self):
        actions = {aid: (label, cb) for aid, label, cb in self._shortcut_actions()}
        self._shortcut_map = []
        for aid, seq in self.shortcuts.items():
            if aid == "capture":
                continue  # global helper hotkey, not a Tk binding
            entry = actions.get(aid)
            parsed = self._parse_shortcut(str(seq)) if entry else None
            if parsed:
                mods, key = parsed
                self._shortcut_map.append((mods, key, entry[0], entry[1]))
        if self.global_hotkeys is not None:
            mapping = {
                aid: str(seq)
                for aid, seq in self.shortcuts.items()
                if aid in actions and aid != "capture" and str(seq).strip()
            }
            try:
                if mapping:
                    errors = self.global_hotkeys.start(mapping)
                else:
                    self.global_hotkeys.stop()
                    errors = []
            except Exception:  # noqa: BLE001
                errors = []
            if errors:
                labels = {aid: label for aid, label, _cb in self._shortcut_actions()}
                names = "、".join(labels.get(aid, aid) for aid in errors)
                self.status.configure(
                    text=f"快捷键被其它程序占用：{names}；请在「快捷键」里换一个组合"
                )

    def _poll_global_hotkeys(self):
        if getattr(self, "_shortcut_capture_active", False):
            self.root.after(150, self._poll_global_hotkeys)
            return
        if self.global_hotkeys is not None:
            actions = {aid: cb for aid, _label, cb in self._shortcut_actions()}
            labels = {aid: label for aid, label, _cb in self._shortcut_actions()}
            if not hasattr(self, "_last_action_time"):
                self._last_action_time = {}
            for action in self.global_hotkeys.poll():
                now = time.time()
                if now - self._last_action_time.get(action, 0) < 0.35:
                    continue
                self._last_action_time[action] = now
                self._log_editor(f"poll action={action}")
                callback = actions.get(action)
                if callback:
                    callback()
        self.root.after(150, self._poll_global_hotkeys)

    def _toast(self, text: str):
        """Small top-most confirmation so a global shortcut is visible even
        when the editor is behind the browser/WPS."""
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            tk.Label(top, text=text, bg="#1a73e8", fg="white",
                     padx=12, pady=6, font=(FONT, 10)).pack()
            x, y = self.root.winfo_pointerxy()
            top.geometry(f"+{x + 18}+{y + 18}")
            top.after(1000, top.destroy)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _log_editor(message: str) -> None:
        try:
            folder = config_path().parent / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "editor.log").open("a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
        except OSError:
            pass

    @staticmethod
    def _parse_shortcut(seq: str):
        parts = [p.strip() for p in seq.split("+") if p.strip()]
        if len(parts) < 2:
            if len(parts) == 1 and parts[0].lower() not in (
                "ctrl", "control", "alt", "shift", "win", "meta"
            ):
                return frozenset(), parts[0].lower()
            return None
        key = parts[-1]
        if key.lower() in ("ctrl", "control", "alt", "shift", "win", "meta"):
            return None
        mods = set()
        for part in parts[:-1]:
            low = part.lower()
            if low in ("ctrl", "control"):
                mods.add("Ctrl")
            elif low == "shift":
                mods.add("Shift")
            elif low in ("alt", "option"):
                mods.add("Alt")
            elif low in ("win", "meta"):
                mods.add("Win")
        return frozenset(mods), key.lower()

    def _on_key(self, event):
        if getattr(self, "_shortcut_capture_active", False):
            return None
        state = int(event.state)
        mods = set()
        if state & 0x4:
            mods.add("Ctrl")
        if state & 0x1:
            mods.add("Shift")
        if state & (0x8 | 0x20000):
            mods.add("Alt")
        key = event.keysym.lower()
        for want_mods, want_key, label, callback in getattr(self, "_shortcut_map", []):
            if want_key == key and want_mods == mods:
                self.status.configure(text=f"快捷键触发：{label}")
                callback()
                return "break"
        return None

    def _capture_shortcut(self, event, entry: tk.Entry):
        """Sticky builder: press Ctrl, then Alt, then 7 -> Ctrl+Alt+7."""
        sym = event.keysym
        mods = list(getattr(entry, "_mods", []))
        modifier = {
            "Control_L": "Ctrl", "Control_R": "Ctrl",
            "Shift_L": "Shift", "Shift_R": "Shift",
            "Alt_L": "Alt", "Alt_R": "Alt",
            "Super_L": "Win", "Super_R": "Win",
        }.get(sym)

        entry.configure(state="normal")
        if sym in ("BackSpace", "Delete", "Escape"):
            entry.delete(0, "end")
            entry._mods = []
            entry.configure(state="readonly")
            return "break"
        if sym in ("Return", "KP_Enter"):
            entry._mods = []
            entry.configure(state="readonly")
            return "break"

        if modifier:
            if modifier not in mods:
                mods.append(modifier)
            entry._mods = mods
            entry.delete(0, "end")
            entry.insert(0, "+".join(mods))
            entry.configure(state="readonly")
            return "break"

        key = sym
        if len(key) == 1:
            key = key.upper() if "Shift" in mods else key.lower()
        entry.delete(0, "end")
        entry.insert(0, "+".join(mods + [key]))
        entry._mods = []
        entry.configure(state="readonly")
        return "break"

    def check_updates(self, silent: bool = False):
        self.status.configure(text="正在检查更新…")

        def worker():
            try:
                from update_checker import check_for_update

                result = check_for_update()
            except Exception:  # noqa: BLE001
                result = None
            self.root.after(0, lambda: self._show_update_result(result, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_result(self, result, silent: bool):
        if result is None:
            if not silent:
                self.status.configure(text="检查更新失败（网络或仓库未配置）")
            return
        if not result.get("newer"):
            self.status.configure(text=f"已是最新版 {result.get('current')}")
            if not silent:
                messagebox.showinfo(
                    "检查更新",
                    f"当前已是最新版：{result.get('current')}",
                    parent=self.root,
                )
            return
        self.status.configure(
            text=f"发现新版本 {result.get('latest')}（当前 {result.get('current')}）"
        )
        messagebox.showinfo(
            "发现新版本",
            f"最新版本：{result.get('latest')}\n"
            f"当前版本：{result.get('current')}\n\n"
            "将在浏览器打开下载页面。",
            parent=self.root,
        )
        url = result.get("url")
        if url:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

    def open_keep_settings(self):
        cfg = load_config()
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.transient(self.root)
        win.grab_set()

        var = tk.StringVar(value=str(cfg.get("keep_captures", 200)))
        box = tk.Frame(win)
        box.pack(padx=16, pady=14)
        tk.Label(box, text="保留最近记录条数：").pack(side="left")
        tk.Spinbox(box, from_=1, to=9999, width=8, textvariable=var).pack(side="left")

        def save():
            try:
                keep = int(var.get())
                if keep < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("设置", "必须是正整数", parent=win)
                return
            cfg["keep_captures"] = keep
            try:
                save_config(cfg)
            except OSError as exc:
                messagebox.showerror("设置", f"写入配置失败：{exc}", parent=win)
                return
            win.destroy()
            self.apply_keep(keep)
            self.status.configure(text=f"设置已保存：保留最近 {keep} 条")

        buttons = tk.Frame(win)
        buttons.pack(pady=(0, 12))
        tk.Button(buttons, text="保存", command=save, width=10).pack(side="left", padx=6)
        tk.Button(buttons, text="取消", command=win.destroy, width=10).pack(side="left", padx=6)
        tk.Button(buttons, text="导出诊断", command=self.export_diagnostics,
                  width=10).pack(side="left", padx=6)
        tk.Button(buttons, text="检查更新", command=self.check_updates,
                  width=10).pack(side="left", padx=6)

        danger = tk.Frame(win)
        danger.pack(pady=(0, 4))
        tk.Button(danger, text="清空所有记录", command=self.clear_all_records,
                  fg="#b00020").pack()
        tk.Label(win, text=f"FixMath {APP_VERSION} · 全离线 · 日志：{config_path().parent / 'logs'}",
                 fg="#888").pack(pady=(0, 10))

    def clear_all_records(self):
        if not messagebox.askyesno(
            "清空所有记录",
            "将永久删除所有截图记录（图片、LaTeX、历史）。\n"
            "这个操作不可恢复，确定继续？",
            parent=self.root,
        ):
            return
        removed = 0
        if self.folder.is_dir():
            for item in list(self.folder.iterdir()):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
        for name in ("latest.json", "latest.png"):
            try:
                (self.folder / name).unlink(missing_ok=True)
            except OSError:
                pass
        self.entries = []
        self.index = -1
        self.reload()
        self.status.configure(text=f"已清空 {removed} 条记录")

    def export_diagnostics(self):
        import zipfile

        target = self.folder.parent / time.strftime("FixMath-diagnostics-%Y%m%d-%H%M%S.zip")
        logs = self.folder.parent / "logs"
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                if logs.is_dir():
                    for log in logs.glob("*.log"):
                        zf.write(log, f"logs/{log.name}")
                cfg = config_path()
                if cfg.is_file():
                    zf.write(cfg, "config.json")
                for entry in self.entries[-20:]:
                    meta = entry / "meta.json"
                    if meta.is_file():
                        zf.write(meta, f"meta/{entry.name}.json")
        except OSError as exc:
            messagebox.showerror("导出诊断", f"写入失败：{exc}", parent=self.root)
            return
        self.status.configure(text=f"诊断包：{target.name}")
        try:
            os.startfile(target.parent)  # noqa: S606 - local explorer
        except OSError:
            pass

    def open_shortcuts(self):
        win = tk.Toplevel(self.root)
        win.title("快捷键设置：依次按 Ctrl/Alt/Shift，再按主键；Delete 清除")
        win.transient(self.root)
        win.grab_set()

        # While capturing keys, do not let the existing global shortcuts (or
        # the screenshot hotkey) fire in the background.
        pause_file = config_path().parent / "pause_capture"
        try:
            pause_file.write_text("1", encoding="utf-8")
        except OSError:
            pass
        self._shortcut_capture_active = True
        if self.global_hotkeys is not None:
            self.global_hotkeys.stop()

        def close():
            try:
                pause_file.unlink(missing_ok=True)
            except OSError:
                pass
            self._shortcut_capture_active = False
            self._apply_shortcuts()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)

        body = tk.Frame(win)
        body.pack(padx=16, pady=12, fill="both", expand=True)
        entries: dict[str, tk.Entry] = {}
        for row, (aid, label, _cb) in enumerate(self._shortcut_actions()):
            tk.Label(body, text=label, anchor="w").grid(
                row=row, column=0, sticky="w", pady=2, padx=(0, 12)
            )
            entry = tk.Entry(body, width=24, justify="center")
            initial = str(self.shortcuts.get(aid, ""))
            if aid == "capture" and not initial:
                initial = str(load_config().get("hotkey") or "Ctrl+Alt+7")
            entry.insert(0, initial)
            entry.configure(state="readonly")
            entry.grid(row=row, column=1, pady=2)
            entry.bind("<KeyPress>", lambda ev, e=entry: self._capture_shortcut(ev, e))
            entry.bind("<Button-1>", lambda ev, e=entry: (e.focus_set(), "break")[1])
            entries[aid] = entry

        probe_box = tk.LabelFrame(win, text="按键检测")
        probe_box.pack(padx=16, pady=(4, 8), fill="x")
        probe = tk.Entry(probe_box, width=28, justify="center")
        probe.pack(pady=(8, 2))
        probe_result = tk.Label(
            probe_box, text="点这里后按下组合键，检测是否能触发", fg="#666"
        )
        probe_result.pack(pady=(0, 8))
        labels = {aid: label for aid, label, _cb in self._shortcut_actions()}

        def probe_key(event):
            mods = []
            state = int(event.state)
            if state & 0x4:
                mods.append("Ctrl")
            if state & 0x1:
                mods.append("Shift")
            if state & (0x8 | 0x20000):
                mods.append("Alt")
            sym = event.keysym
            if sym in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                       "Alt_L", "Alt_R", "Super_L", "Super_R"):
                probe.delete(0, "end")
                probe.insert(0, "+".join(mods) if mods else "…")
                return "break"
            key = sym
            if len(key) == 1:
                key = key.upper() if "Shift" in mods else key.lower()
            combo = "+".join(mods + [key])
            probe.delete(0, "end")
            probe.insert(0, combo)
            hit = None
            for aid, entry in entries.items():
                if entry.get().strip().lower() == combo.lower():
                    hit = labels[aid]
                    break
            if hit:
                probe_result.configure(text=f"✅ {combo} → {hit}（保存后生效）",
                                       fg="#1b7f3a")
            else:
                probe_result.configure(text=f"❌ {combo} 未绑定（系统可能吞掉该组合）",
                                       fg="#c62828")
            return "break"

        probe.bind("<KeyPress>", probe_key)
        probe.bind("<Button-1>", lambda _e: (probe.focus_set(), "break")[1])

        def save():
            data = {
                aid: entry.get().strip()
                for aid, entry in entries.items()
                if entry.get().strip()
            }
            self.shortcuts = data
            cfg = load_config()
            cfg["shortcuts"] = data
            capture = data.get("capture", "").strip()
            if capture:
                cfg["hotkey"] = capture.lower()
            else:
                cfg.pop("hotkey", None)
            try:
                save_config(cfg)
            except OSError as exc:
                messagebox.showerror("快捷键", f"写入配置失败：{exc}", parent=win)
                return
            close()
            self.status.configure(text=f"快捷键已保存（{len(data)} 条）")
            errors = getattr(self.global_hotkeys, "errors", []) if self.global_hotkeys else []
            labels = {aid: label for aid, label, _cb in self._shortcut_actions()}
            text = f"已保存 {len(data)} 条快捷键。\n"
            if capture:
                text += f"截图热键 {capture} 约 1 秒后生效。\n"
            if errors:
                text += "以下全局快捷键注册失败（被占用）：\n" + "\n".join(
                    f"· {labels.get(a, a)}" for a in errors
                )
            messagebox.showinfo("快捷键", text, parent=self.root)

        buttons = tk.Frame(win)
        buttons.pack(pady=(0, 12))
        tk.Label(
            win,
            text="提示：Ctrl+Alt+数字常被输入法占用，建议用 F1–F12 或 Ctrl+Shift+字母",
            fg="#888",
        ).pack(pady=(0, 6))
        tk.Button(buttons, text="保存", command=save, width=10).pack(side="left", padx=6)
        tk.Button(buttons, text="取消", command=close, width=10).pack(side="left", padx=6)

    def apply_keep(self, keep: int):
        folders = sorted(self.entries)
        if len(folders) <= keep:
            return
        old = folders[: len(folders) - keep]
        if not messagebox.askyesno(
            "清理记录",
            f"将永久删除 {len(old)} 条旧记录，只保留最近 {keep} 条。确定？",
            parent=self.root,
        ):
            return
        for folder in old:
            shutil.rmtree(folder, ignore_errors=True)
        self.reload()

    def open_transparent(self):
        if not (0 <= self.index < len(self.entries)):
            return
        path = self.entries[self.index] / "transparent.png"
        if path.is_file():
            os.startfile(path.parent)  # noqa: S606 - local explorer
        else:
            messagebox.showinfo("透明图", "这条记录还没有 transparent.png", parent=self.root)

    def open_folder(self):
        if 0 <= self.index < len(self.entries):
            os.startfile(self.entries[self.index])  # noqa: S606 - local explorer

    def on_close(self):
        self.save(silent=True)
        if self.global_hotkeys is not None:
            self.global_hotkeys.stop()
        self.root.destroy()

    # ---------------------------------------------------------------- watch
    def watch_latest(self):
        if not self.watch:
            return
        latest = self.folder / "latest.json"
        try:
            mtime = latest.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime > self._latest_mtime:
            if self._latest_mtime != 0.0 and self.dirty:
                self.save(silent=True)
            self._latest_mtime = mtime
            self.reload(select_last=True)
        self.root.after(1200, self.watch_latest)


def enable_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass


def _install_exception_handler(root: tk.Tk) -> None:
    def handler(exc_type, exc, tb):
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc, tb))
        path = config_path().parent / "logs" / "editor.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        except OSError:
            path = None
        messagebox.showerror(
            "FixMath 错误",
            f"{exc}\n\n详细日志：{path}" if path else str(exc),
            parent=root,
        )

    root.report_callback_exception = handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FixMath editor MVP")
    ap.add_argument("--captures", default=str(default_captures()))
    ap.add_argument("--no-watch", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="open, load one entry, print and exit")
    args = ap.parse_args(argv)

    enable_dpi_awareness()
    if ttkb is not None:
        root = ttkb.Window(themename="flatly")
    else:
        root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    editor = Editor(root, Path(args.captures).expanduser(), watch=not args.no_watch)
    _install_exception_handler(root)
    editor._latest_mtime = 0.0
    if args.smoke:
        root.update()
        print("entries:", len(editor.entries))
        print("index:", editor.index)
        print("latex:", editor.current_text()[:120])
        root.destroy()
        return 0
    root.after(1200, editor.watch_latest)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
