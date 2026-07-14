from __future__ import annotations

import logging
import time
from pathlib import Path

from .settings import LOG_DIR


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"

    logger = logging.getLogger("apk_migrate_tui")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    logger.addHandler(fh)

    logger.info("Log file: %s", log_file)
    return logger


def log_file_path() -> Path | None:
    logger = logging.getLogger("apk_migrate_tui")
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            return Path(h.baseFilename)
    return None
