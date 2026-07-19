from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PHONEISH_RE = re.compile(r"(?<!\d)(?:\+?7|8)(?:[\s()\-.]*\d){10}(?!\d)")
TELEGRAM_TOKEN_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+", re.IGNORECASE)
TELEGRAM_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{10,}")


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value = super().format(record)
        value = PHONEISH_RE.sub("+7***REDACTED***", value)
        value = TELEGRAM_TOKEN_URL_RE.sub(r"\1***REDACTED***", value)
        return TELEGRAM_TOKEN_RE.sub("***TELEGRAM_TOKEN_REDACTED***", value)


def configure_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console_formatter = RedactingFormatter("%(levelname)s: %(message)s")

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    # pythonw.exe has no stderr. Skipping this handler avoids repeated logging
    # errors in the Windows background controller while preserving the file log.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(console_formatter)
        root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "avito-crm.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
