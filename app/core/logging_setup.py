"""Structured logging with a mandatory redaction filter.

The filter is attached to the root handler, so any logger in the app is safe
by default: it is not possible to log a raw secret without deliberately
removing the filter.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.security.redact import redact

_CONFIGURED = False


class RedactionFilter(logging.Filter):
    """Scrubs the message, args and any string extras of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        for key, value in list(record.__dict__.items()):
            if key.startswith("_") or key in {
                "msg",
                "args",
                "exc_info",
                "exc_text",
                "stack_info",
            }:
                continue
            if isinstance(value, str):
                record.__dict__[key] = redact(value)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("user_id", "task", "module", "duration_ms", "status", "extra"):
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
        message = record.getMessage()
        extras = [
            f"{k}={record.__dict__[k]}"
            for k in ("user_id", "task", "module", "status")
            if record.__dict__.get(k) is not None
        ]
        line = base + message + (" | " + " ".join(extras) if extras else "")
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        for handler in root.handlers:
            handler.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    handler.addFilter(RedactionFilter())
    handler.setLevel(level.upper())

    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party noise down, but keep them redacted through the root handler.
    for noisy in ("httpx", "httpcore", "urllib3", "telegram.ext.Updater", "LiteLLM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
