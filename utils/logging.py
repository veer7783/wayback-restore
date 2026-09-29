"""Structured logging that never writes credentials."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

_SECRET_KEYS = ("password", "passwd", "secret", "token", "authorization", "api_key")
_SECRET_RE = re.compile(
    r"(?i)((?:password|passwd|secret|token|authorization|api[_-]?key)\s*[:=]\s*)(\S+)"
)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return _SECRET_RE.sub(r"\1***", message)


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("pkh")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "restorer.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def redact_mapping(data: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in data.items():
        if any(secret in key.lower() for secret in _SECRET_KEYS):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
