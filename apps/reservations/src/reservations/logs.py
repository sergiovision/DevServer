"""Structured JSON logging with request/job context carried in contextvars."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Iterator
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get() or {})


def set_context(**fields: Any) -> Token[dict[str, Any] | None]:
    """Replace the log context of the current task; reset with :func:`reset_context`."""
    return _CONTEXT.set({k: str(v) for k, v in fields.items() if v is not None})


def reset_context(token: Token[dict[str, Any] | None]) -> None:
    _CONTEXT.reset(token)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    ctx = current_context()
    ctx.update({k: str(v) for k, v in fields.items() if v is not None})
    token = _CONTEXT.set(ctx)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(current_context())
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
