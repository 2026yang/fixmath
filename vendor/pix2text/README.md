# Optional local models

This directory is intentionally not committed to Git.

- `1.1/mfd-1.5-onnx/` — Pix2Text formula detector (about 80 MB), used by the
  page-level formula detection feature.
- `1.1/mfr-1.5-onnx/` — Pix2Text formula recognizer; by default the app reads
  it from `%APPDATA%\pix2text`.

DeepSeek / FixMath can download `mfd-1.5` on first use:

```python
from pix2text import MathFormulaDetector
MathFormulaDetector(model_name="mfd-1.5", model_backend="onnx",
                    root="vendor/pix2text")
```

Do not commit model weights to the repository or to a normal GitHub Release;
host them as a Release asset or download them on first run.
