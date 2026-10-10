"""User-facing error classification.

Internal exceptions are logged (redacted) with full detail; the user and the
language model receive a short, safe message without stack traces,
hostnames or credentials.
"""

from __future__ import annotations

import logging

from security.redaction import redact

logger = logging.getLogger(__name__)


class AppError(Exception):
    """An error whose message is safe to show to the user."""

    def __init__(self, message: str, *, category: str = "error") -> None:
        super().__init__(message)
        self.user_message = message
        self.category = category


class SQLValidationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, category="sql_rejected")


class DataSourceError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, category="data_source")


_DB_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("password authentication failed",), "Database login failed. Check PG_USER and PG_PASSWORD in .env."),
    (("could not translate host", "name or service not known"), "Database host not found. Check PG_HOST in .env."),
    (("connection refused", "could not connect", "timeout expired"),
     "Could not reach PostgreSQL. Is the server running and PG_HOST/PG_PORT correct?"),
    (('database "', "does not exist"), "The configured database does not exist. Check PG_DATABASE."),
    (("canceling statement due to statement timeout",),
     "The query exceeded the statement timeout. Try narrowing the date range or adding filters."),
    (("permission denied",), "The read-only database role does not have permission for that object."),
    (("read-only transaction",), "Blocked: the database session is read-only."),
    (("does not exist",), "A referenced table, column or function does not exist. Check names with the Database Explorer."),
    (("invalid input syntax for type",),
     "Some stored values cannot be converted to numbers or geometry (for example '#NAME?' or '#VALUE!' left by a "
     "spreadsheet formula). Fix those rows at source, or filter them out in the query."),
    (("syntax error",), "PostgreSQL reported a syntax error in the query."),
    (("division by zero",), "The query divided by zero. Add a NULLIF guard to the denominator."),
]


def to_user_message(exc: BaseException, context: str = "operation") -> str:
    """Log `exc` in full (redacted) and return a safe user-facing message."""
    if isinstance(exc, AppError):
        logger.info("%s rejected: %s", context, exc.user_message)
        return exc.user_message
    logger.exception("%s failed", context, exc_info=exc)
    text = str(exc).lower()
    for needles, message in _DB_HINTS:
        if all(n in text for n in needles):
            return message
    # Return only the first line of a database error, redacted, without the
    # SQLAlchemy "background on this error" boilerplate.
    first_line = redact(str(exc).splitlines()[0]) if str(exc) else type(exc).__name__
    if "(psycopg" in first_line:
        first_line = first_line.split(")", 1)[-1].strip()
    return f"The {context} failed: {first_line[:300]}"
