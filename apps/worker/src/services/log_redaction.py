"""Redact secrets from log output.

A logging filter that masks Telegram bot tokens so they never leak into
log files or stdout. The full Telegram API URL
``https://api.telegram.org/bot<token>/`` is replaced with
``https://api.telegram.org/.../`` — this also shrinks noisy httpx request
lines that would otherwise embed the long token on every poll.
"""

import logging
import re

# ``bot<token>`` where <token> is ``<id>:<secret>`` — match everything up to
# the next slash so the trailing path (e.g. ``/getUpdates``) is preserved.
_TELEGRAM_TOKEN_RE = re.compile(r"(https://api\.telegram\.org/)bot[^/\s]+")
_TELEGRAM_REPLACEMENT = r"\1..."


def redact(text: str) -> str:
    """Return *text* with any Telegram bot token masked."""
    return _TELEGRAM_TOKEN_RE.sub(_TELEGRAM_REPLACEMENT, text)


class RedactingFilter(logging.Filter):
    """Logging filter that strips secrets out of every formatted record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            # Overwrite msg with the already-formatted, redacted text and drop
            # args so the handler does not re-interpolate (and re-leak) them.
            record.msg = redacted
            record.args = None
        return True


def install_redaction() -> None:
    """Attach the redacting filter to every configured handler.

    Covers the root logger plus any named loggers (e.g. uvicorn's) that own
    their own handlers. Idempotent — a handler already carrying a
    ``RedactingFilter`` is left untouched, so repeated calls are safe.
    """
    loggers = [logging.getLogger()]
    loggers += [
        lg
        for lg in logging.Logger.manager.loggerDict.values()
        if isinstance(lg, logging.Logger)
    ]
    for lg in loggers:
        for handler in lg.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
