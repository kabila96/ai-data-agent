"""Logging setup with secret redaction.

Every log record passes through `RedactingFilter`, which masks connection
strings, password assignments, API keys and the literal secret values loaded
from settings. Full tracebacks go to a local log file only; the UI shows
classified, friendly messages (see `security.errors`).
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # scheme://user:password@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s]+):([^@\s]+)@"), r"\1:***@"),
    # password=..., pwd=..., PGPASSWORD=...
    (re.compile(r"(?i)\b(password|passwd|pwd|pgpassword)\s*[=:]\s*([^\s,;'\"]+)"), r"\1=***"),
    # OpenAI-style keys
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    # Authorization headers
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1***"),
]

_secret_values: list[str] = []


def register_secrets(values: list[str]) -> None:
    """Register literal secret values that must never appear in logs."""
    for value in values:
        if value and len(value) >= 4 and value not in _secret_values:
            _secret_values.append(value)


def redact(text: str) -> str:
    """Return `text` with secrets masked."""
    if not text:
        return text
    for secret in _secret_values:
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class _ConsoleFormatter(logging.Formatter):
    def formatException(self, ei) -> str:  # noqa: N802
        return ""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text
        record.exc_info, record.exc_text = None, None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved


def configure_logging(log_dir: Path, level: str = "INFO", secrets: list[str] | None = None) -> None:
    """Idempotently configure root logging with redaction on every handler."""
    register_secrets(secrets or [])
    root = logging.getLogger()
    if getattr(root, "_ai_agent_configured", False):
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    console = logging.StreamHandler()
    console.setLevel(logging.ERROR)  # the console stays quiet; details go to app.log
    file_handler.setFormatter(fmt)
    console.setFormatter(_ConsoleFormatter("%(levelname)s %(name)s: %(message)s"))
    for handler in (file_handler, console):
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    # Third-party libraries can be chatty and may echo request details.
    for noisy in ("httpx", "httpx2", "openai", "urllib3", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._ai_agent_configured = True  # type: ignore[attr-defined]
