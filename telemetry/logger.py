"""Structured JSON logging configuration.

All agent components should log through :func:`get_logger` so log output is
consistent, structured, and easy to grep during a live demo.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

_REDACTED_KEYS = {"token", "api_key", "authorization", "secret", "password", "github_token"}


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(_redact(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact keys that look like secrets before logging."""
    redacted: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _REDACTED_KEYS:
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = _redact(value)
        else:
            redacted[key] = value
    return redacted


def get_logger(name: str) -> logging.Logger:
    """Return a configured structured logger for ``name``.

    The log level is controlled by the ``AGENT_LOG_LEVEL`` environment
    variable (default ``INFO``).
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    level_name = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    return logger
