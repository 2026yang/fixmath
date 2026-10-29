"""Download the mfr-1.5 OCR model without importing torchvision directly.

Some torch/torchvision wheels installed by pip are mismatched and crash on
``import torchvision`` (``operator torchvision::nms does not exist``).  The OCR
server already installs a tiny torchvision stub before importing pix2text, so
reuse that path here.
"""

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "ocr"))

import recognize_server  # noqa: E402  (installs the torchvision stub)

recognize_server.load_model()
print("model ready")
