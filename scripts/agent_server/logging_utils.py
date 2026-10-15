"""Structured JSON logging helpers for Railway-friendly observability."""
from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_request_context: ContextVar[dict[str, Any]] = ContextVar("request_context", default={})


class JsonFormatter(logging.Formatter):
    """Render each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }

        context = _request_context.get()
        if context:
            payload.update(context)

        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({key: value for key, value in extra.items() if value is not None})

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging() -> None:
    """Configure root/app logging for Railway structured log ingestion."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def set_request_context(**fields: Any):
    """Replace the active request-scoped logging context."""
    sanitized = {key: value for key, value in fields.items() if value is not None}
    return _request_context.set(sanitized)


def update_request_context(**fields: Any) -> None:
    """Merge fields into the active request-scoped logging context."""
    current = dict(_request_context.get())
    current.update({key: value for key, value in fields.items() if value is not None})
    _request_context.set(current)


def clear_request_context(token) -> None:
    """Reset the request-scoped logging context."""
    _request_context.reset(token)


def get_request_context() -> dict[str, Any]:
    """Return a copy of the active request-scoped logging context."""
    return dict(_request_context.get())


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    exc_info=None,
    stack_info: bool = False,
    **fields: Any,
) -> None:
    """Emit a structured log with extra Railway-queryable attributes."""
    logger.log(
        level,
        message,
        exc_info=exc_info,
        stack_info=stack_info,
        extra={"extra_fields": {key: value for key, value in fields.items() if value is not None}},
    )
