# Models

- `1.1/mfr-1.5-onnx/` — Pix2Text LaTeX recognizer, **committed to this
  repository** so a source ZIP works offline.
- `1.1/mfd-1.5-onnx/` — optional formula detector (about 80 MB), not
  committed; download it with `MathFormulaDetector` when needed.

If the recognizer is missing, the app falls back to
`%APPDATA%\pix2text\1.1\mfr-1.5-onnx` and can download it on first use.
