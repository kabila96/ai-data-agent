"""Execution of validated, read-only SQL.

Every query:
1. passes `SQLValidator` (single SELECT, approved objects, row cap);
2. runs inside `SET TRANSACTION READ ONLY` with `SET LOCAL statement_timeout`;
3. is rolled back afterwards;
4. is recorded in the audit log (SQL text, verdict, row count, timing; never row data).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import Engine, bindparam, text

from config import Settings
from database.metadata import MetadataService
from models.schemas import SQLValidationResult
from security.errors import SQLValidationError, to_user_message
from security.sql_validator import SQLValidator
from storage.app_store import AppStore

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    sql: str
    data: pd.DataFrame
    row_count: int
    truncated: bool
    elapsed_ms: int
    tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class QueryExecutor:
    def __init__(
        self,
        engine: Engine,
        metadata: MetadataService,
        settings: Settings,
        store: AppStore | None = None,
    ) -> None:
        self.engine = engine
        self.metadata = metadata
        self.settings = settings
        self.store = store

    def validator(self, max_rows: int | None = None) -> SQLValidator:
        return SQLValidator(
            self.metadata.catalog(),
            max_rows=min(max_rows or self.settings.max_rows, self.settings.max_rows),
            database_name=self.settings.pg_database,
        )

    def validate(self, sql: str, max_rows: int | None = None) -> SQLValidationResult:
        result = self.validator(max_rows).validate(sql)
        if self.store is not None:
            self.store.audit(
                sql=sql,
                verdict="accepted" if result.ok else "rejected",
                detail="; ".join(result.errors) or None,
            )
        return result

    def run(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        max_rows: int | None = None,
        source: str = "agent",
    ) -> QueryResult:
        """Validate and execute `sql`. Raises `SQLValidationError` if rejected."""
        validation = self.validate(sql, max_rows)
        if not validation.ok or not validation.safe_sql:
            raise SQLValidationError("Query rejected: " + "; ".join(validation.errors))
        return self._execute(validation, params or {}, source)

    def run_wrapped(
        self,
        validated_inner_sql: str,
        where_sql: str,
        params: dict[str, Any],
        source: str = "dashboard",
    ) -> QueryResult:
        """Apply application-built filters around an already-validated query.

        `where_sql` must be built by the application from quoted identifiers and
        bound parameter names only (see `dashboards.filters`). The combined query
        is validated again before execution.
        """
        inner = validated_inner_sql.rstrip().rstrip(";")
        wrapped = f"SELECT * FROM ({inner}) AS _f" + (f" WHERE {where_sql}" if where_sql else "")
        return self.run(wrapped, params=params, source=source)

    def _execute(
        self, validation: SQLValidationResult, params: dict[str, Any], source: str
    ) -> QueryResult:
        sql = validation.safe_sql or ""
        timeout = int(self.settings.statement_timeout_ms)
        started = time.perf_counter()
        status, detail, rows = "success", None, 0
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text(f"SET LOCAL statement_timeout = {timeout}"))
                statement = text(sql)
                expanding = [k for k, v in params.items() if isinstance(v, (list, tuple, set))]
                if expanding:
                    statement = statement.bindparams(
                        *[bindparam(k, expanding=True) for k in expanding]
                    )
                    params = {k: (list(v) if k in expanding else v) for k, v in params.items()}
                cursor = conn.execute(statement, params)
                columns = list(cursor.keys())
                records = cursor.fetchall()
                conn.rollback()
            data = pd.DataFrame.from_records(records, columns=columns)
            rows = len(data)
        except Exception as exc:
            status, detail = "error", to_user_message(exc, "query")
            raise SQLValidationError(detail) from exc
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            if self.store is not None:
                self.store.audit(
                    sql=sql, verdict="executed" if status == "success" else "failed",
                    detail=detail, row_count=rows, elapsed_ms=elapsed, source=source,
                )
        warnings = list(validation.warnings)
        truncated = validation.row_cap is not None and rows >= validation.row_cap
        if truncated:
            warnings.append(
                f"Result reached the {validation.row_cap:,}-row limit and may be incomplete. "
                "Aggregate in SQL or add filters."
            )
        if rows == 0:
            warnings.append("The query returned no rows.")
        return QueryResult(
            sql=sql, data=_coerce_types(data), row_count=rows, truncated=truncated,
            elapsed_ms=elapsed, tables=validation.tables, warnings=warnings,
        )


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Convert Decimal columns to float so pandas/Plotly can work with them."""
    from decimal import Decimal

    for col in df.columns:
        series = df[col]
        if series.dtype == object:
            sample = series.dropna().head(50)
            if len(sample) and all(isinstance(v, Decimal) for v in sample):
                df[col] = pd.to_numeric(series, errors="coerce")
    return df
