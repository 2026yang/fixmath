"""Small file+console logger so a helper started by double-click is diagnosable."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def setup_logging() -> tuple[logging.Logger, Path]:
    base = os.environ.get("FIXMATH_HOME") or os.environ.get("LOCALAPPDATA") \
        or str(Path.home() / "AppData" / "Local")
    folder = Path(base) / "FixMath"

    logger = logging.getLogger("fixmath")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    path = folder / "helper.log"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(folder / "helper.old.log")
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(file_handler)
    except OSError:
        path = Path("(helper.log unavailable)")

    return logger, path
