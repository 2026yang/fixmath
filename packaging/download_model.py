"""Ensure the mfr-1.5 OCR model exists in %APPDATA%\\pix2text."""

from pix2text.latex_ocr import LatexOCR

LatexOCR(
    model_backend="onnx",
    device="cpu",
    more_model_configs={"use_merged": False, "use_cache": False},
)
print("model ready")
