"""FixMath 离线公式识别服务（Pix2Text / mfr-1.5 ONNX）。

启动后常驻，POST /ocr 传图片路径或 PNG 字节，返回 LaTeX。

    py -3.11 recognize_server.py --port 8765

首次启动只加载本地模型（%APPDATA%\\pix2text\\1.1\\mfr-1.5-onnx），不联网。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

MODEL_LOCK = threading.Lock()
MODEL = None
_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_MAX = 32


def _stub_torchvision() -> None:
    """pix2text imports torchvision.utils.save_image at import time.

    In the frozen build torchvision's native ``_C`` extension is not usable,
    which raises when torchvision registers its custom ops.  The ONNX
    inference path never calls torchvision, so install a tiny stub instead.
    """
    import sys
    import types

    if "torchvision" in sys.modules:
        return
    try:
        import torchvision  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    tv = types.ModuleType("torchvision")
    tv.__path__ = []
    tv.__version__ = "0.0.0"
    tv.__file__ = "torchvision_stub.py"
    tv.__package__ = "torchvision"
    import importlib.machinery

    tv.__spec__ = importlib.machinery.ModuleSpec("torchvision", None, is_package=True)
    tv.__spec__.submodule_search_locations = []

    class _Dummy:
        def __init__(self, *_args, **_kwargs):
            pass

        def __call__(self, *_args, **_kwargs):
            return _Dummy()

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return _Dummy()

    def make_module(name: str):
        module = types.ModuleType(name)
        module.__path__ = []
        module.__file__ = "torchvision_stub.py"
        module.__spec__ = None
        module.__loader__ = None

        def getattr_impl(attr):
            if attr.startswith("__"):
                raise AttributeError(attr)
            return _Dummy()

        module.__getattr__ = getattr_impl
        sys.modules[name] = module
        return module

    utils = make_module("torchvision.utils")
    utils.save_image = lambda *_a, **_k: None
    models = make_module("torchvision.models")
    models_utils = make_module("torchvision.models._utils")
    models_utils.IntermediateLayerGetter = _Dummy
    models._utils = models_utils
    transforms = make_module("torchvision.transforms")
    ops = make_module("torchvision.ops")
    datasets = make_module("torchvision.datasets")
    tv.utils = utils
    tv.models = models
    tv.transforms = transforms
    tv.ops = ops
    tv.datasets = datasets

    def tv_getattr(attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Dummy()

    tv.__getattr__ = tv_getattr
    sys.modules["torchvision"] = tv

    # Any remaining ``torchvision.*`` submodule imported by pix2text/cnstd is
    # served as a dummy module instead of chasing each name one by one.
    import importlib.abc
    import importlib.machinery

    class _StubLoader(importlib.abc.Loader):
        def create_module(self, spec):
            module = types.ModuleType(spec.name)
            module.__path__ = []
            module.__file__ = "torchvision_stub.py"
            module.__spec__ = spec
            module.__loader__ = self

            def getattr_impl(attr):
                if attr.startswith("__"):
                    raise AttributeError(attr)
                return _Dummy()

            module.__getattr__ = getattr_impl
            sys.modules[spec.name] = module
            return module

        def exec_module(self, module):
            return None

    class _StubFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, _path=None, _target=None):
            if fullname.startswith("torchvision."):
                return importlib.machinery.ModuleSpec(fullname, _StubLoader(),
                                                      is_package=True)
            return None

    sys.meta_path.insert(0, _StubFinder())
    sys.modules["torchvision.utils"] = utils
    sys.modules["torchvision.models"] = models
    sys.modules["torchvision.transforms"] = transforms
    sys.modules["torchvision.ops"] = ops
    sys.modules["torchvision.datasets"] = datasets


_stub_torchvision()


def _looks_like_text(img: Image.Image) -> bool:
    """Cheap rejection for paragraph screenshots.

    A text paragraph has hundreds of small connected components across
    several text lines; a formula crop has tens.  This runs in a few
    milliseconds and avoids spending ~9 s generating garbage tokens.
    """
    try:
        import numpy as np
        from scipy import ndimage

        gray = np.array(img.convert("L"))
        mask = gray < 128
        if mask.mean() < 0.005 or mask.mean() > 0.45:
            return False
        labels, _ = ndimage.label(mask)
        sizes = np.bincount(labels.ravel())[1:]
        components = int((sizes >= 4).sum())
        rows = mask.sum(1) > max(1, gray.shape[1] * 0.004)
        lines = int(((rows[1:] & ~rows[:-1]).sum()) + (1 if rows[0] else 0))
        return components >= 200 and lines >= 2
    except Exception:  # noqa: BLE001 - never block OCR on the heuristic
        return False


def _patch_transformers_doc() -> None:
    """Frozen builds have no .py sources; transformers' doc decorator calls
    inspect.getsource and crashes.  A constant indentation is enough."""
    try:
        import transformers.utils.doc as tdoc

        tdoc.get_docstring_indentation_level = lambda *_a, **_k: 0
    except Exception:  # noqa: BLE001
        pass


_patch_transformers_doc()


def _bundled_model_root():
    """Return a model root shipped next to the exe, if present."""
    candidates = []
    env = os.environ.get("FIXMATH_MODELS")
    if env:
        candidates.append(Path(env))
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    candidates.extend([base / "models" / "pix2text", base / "vendor" / "pix2text"])
    for root in candidates:
        if (root / "1.1" / "mfr-1.5-onnx").is_dir():
            return root
    return None


def load_model():
    from pix2text.latex_ocr import LatexOCR

    # 模型仓库只有 encoder_model.onnx / decoder_model.onnx，没有 merged 版本；
    # 必须显式关闭 merged 与 kv-cache，否则新版 optimum 会去找
    # decoder_model_merged.onnx 并报错。
    kwargs = {
        "model_backend": "onnx",
        "device": "cpu",
        "more_model_configs": {"use_merged": False, "use_cache": False},
    }
    root = _bundled_model_root()
    if root is not None:
        print(f"[ocr] using bundled models: {root}", flush=True)
        kwargs["root"] = str(root)
    return LatexOCR(**kwargs)


class Handler(BaseHTTPRequestHandler):
    server_version = "FixMathOCR/0.1"

    def log_message(self, fmt, *args):  # noqa: A003 - keep stdout clean
        print(f"[ocr] {self.address_string()} {fmt % args}", flush=True)

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "model": "mfr-1.5-onnx"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ocr":
            self._reply(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").lower()

            if "application/json" in ctype:
                data = json.loads(body.decode("utf-8"))
                img_path = data["path"]
                img = Image.open(img_path).convert("RGB")
                try:
                    stamp = os.path.getmtime(img_path)
                except OSError:
                    stamp = 0
                key = f"path:{img_path}:{stamp}"
            else:
                img = Image.open(io.BytesIO(body)).convert("RGB")
                key = "bytes:" + hashlib.sha256(body).hexdigest()

            t0 = time.perf_counter()
            if _looks_like_text(img):
                ms = (time.perf_counter() - t0) * 1000
                self._reply(200, {
                    "latex": "",
                    "score": 0.0,
                    "ms": round(ms, 1),
                    "skipped": "text",
                })
                return
            with MODEL_LOCK:
                result = _CACHE.get(key)
                if result is None:
                    # Bound runaway generation: a wrong crop can make the model
                    # emit a huge empty array and block the queue for everyone.
                    result = MODEL.recognize(img, rec_config={"max_new_tokens": 512})
                    _CACHE[key] = result
                    while len(_CACHE) > _CACHE_MAX:
                        _CACHE.popitem(last=False)
            ms = (time.perf_counter() - t0) * 1000

            self._reply(200, {
                "latex": result.get("text", ""),
                "score": float(result.get("score", 0.0)),
                "ms": round(ms, 1),
            })
        except Exception as exc:  # noqa: BLE001 - report to the caller
            self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    print("[ocr] loading pix2text mfr-1.5 (onnx, cpu) ...", flush=True)
    global MODEL
    MODEL = load_model()
    try:
        MODEL.recognize(Image.new("RGB", (48, 48), "white"),
                        rec_config={"max_new_tokens": 8})
    except Exception:  # noqa: BLE001 - warmup only
        pass
    print("[ocr] model ready", flush=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[ocr] listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
