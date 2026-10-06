"""Lightweight structured JSON logging (no heavy observability stack).

Usage: ``log.info("user claimed config", user_id=42, campaign="c1")`` —
keyword arguments become structured fields in the JSON output.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "fields",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class StructLogger:
    """Thin adapter turning kwargs into structured `extra` fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(self, level: int, msg: str, exc: BaseException | None = None, **fields) -> None:
        self._logger.log(level, msg, extra={"fields": fields}, exc_info=exc if exc is not None else None)

    def debug(self, msg: str, **fields) -> None:
        self._log(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields) -> None:
        self._log(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields) -> None:
        self._log(logging.WARNING, msg, **fields)

    def error(self, msg: str, exc: BaseException | None = None, **fields) -> None:
        self._log(logging.ERROR, msg, exc=exc, **fields)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "alembic"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> StructLogger:
    return StructLogger(logging.getLogger(f"pv_growth.{name}"))
