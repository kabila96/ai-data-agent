"""Per-session state shared by the agent tools and the Streamlit pages.

Data frames stay in this process. The language model only ever receives the
compact summaries produced by `frame_summary`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from analysis.csv_loader import SemanticType
from config import Settings
from database.executor import QueryExecutor
from database.metadata import MetadataService
from models.schemas import ChartSpec, SourceRecipe, new_id, utc_now
from security.errors import DataSourceError
from storage.app_store import AppStore


@dataclass
class CSVSource:
    name: str
    raw: pd.DataFrame  # every column as text, used to re-apply type overrides
    data: pd.DataFrame  # typed
    types: dict[str, SemanticType]
    warnings: list[str] = field(default_factory=list)
    uploaded_at: datetime = field(default_factory=utc_now)


@dataclass
class ResultRecord:
    id: str
    label: str
    data: pd.DataFrame
    recipe: SourceRecipe
    source_label: str
    sql: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    refreshable: bool = True


@dataclass
class ChartRecord:
    id: str
    spec: ChartSpec
    figure: go.Figure
    result_id: str


@dataclass
class PendingApproval:
    id: str
    kind: str  # "join"
    description: str
    payload: dict[str, Any]


@dataclass
class AgentContext:
    settings: Settings
    store: AppStore
    metadata: MetadataService | None = None
    executor: QueryExecutor | None = None
    csv_sources: dict[str, CSVSource] = field(default_factory=dict)
    results: dict[str, ResultRecord] = field(default_factory=dict)
    charts: dict[str, ChartRecord] = field(default_factory=dict)
    approved_joins: set[tuple[str, str, str, str]] = field(default_factory=set)
    pending_approvals: dict[str, PendingApproval] = field(default_factory=dict)
    export_offers: list[str] = field(default_factory=list)
    selected_sources: list[str] = field(default_factory=lambda: ["database"])
    max_results_kept: int = 60

    # ------------------------------------------------------------ helpers
    @property
    def database_available(self) -> bool:
        return self.executor is not None and self.metadata is not None

    def require_database(self) -> tuple[MetadataService, QueryExecutor]:
        if self.metadata is None or self.executor is None:
            raise DataSourceError("The database is not connected. Check the connection settings on the Database Explorer page.")
        if "database" not in self.selected_sources:
            raise DataSourceError("The database is not selected as a source for this conversation.")
        return self.metadata, self.executor

    def add_result(
        self, data: pd.DataFrame, label: str, recipe: SourceRecipe, source_label: str,
        sql: str | None = None, warnings: list[str] | None = None, refreshable: bool = True,
    ) -> ResultRecord:
        record = ResultRecord(new_id("res"), label, data, recipe, source_label, sql,
                              list(warnings or []), refreshable=refreshable)
        self.results[record.id] = record
        while len(self.results) > self.max_results_kept:
            oldest = next(iter(self.results))
            self.results.pop(oldest)
        return record

    def add_chart(self, spec: ChartSpec, figure: go.Figure, result_id: str) -> ChartRecord:
        record = ChartRecord(new_id("cht"), spec, figure, result_id)
        self.charts[record.id] = record
        return record

    def csv_names(self) -> list[str]:
        return list(self.csv_sources)

    def resolve_frame(self, ref: str) -> tuple[pd.DataFrame, str, SourceRecipe, bool]:
        """Resolve a source reference to (data, label, recipe, refreshable).

        Accepted forms: a result id (`res_...`), `csv:<name>`, or a CSV file name.
        """
        ref = (ref or "").strip()
        if ref in self.results:
            r = self.results[ref]
            return r.data, r.label, r.recipe, r.refreshable
        name = ref[4:] if ref.lower().startswith("csv:") else ref
        if name in self.csv_sources:
            if f"csv:{name}" not in self.selected_sources and "all_csv" not in self.selected_sources:
                raise DataSourceError(f"CSV '{name}' is not selected as a source for this conversation.")
            src = self.csv_sources[name]
            return src.data, f"CSV {name}", SourceRecipe(kind="csv", csv_name=name, description=f"Uploaded CSV {name}"), True
        # An approved database table named directly (e.g. "data_science.hr_attrition"): read it once,
        # through the validator, and register it as a result so analysis tools can use it.
        if self.metadata is not None and self.executor is not None and "database" in self.selected_sources:
            schema, _, table = name.rpartition(".")
            resolved = self.metadata.resolve_table(schema.strip('"') or None, table.strip('"'))
            if resolved is not None:
                from security.sql_validator import quote_ident

                sql = f"SELECT * FROM {quote_ident(resolved[0])}.{quote_ident(resolved[1])}"
                result = self.executor.run(sql, source="agent")
                record = self.add_result(
                    result.data, f"{resolved[0]}.{resolved[1]}",
                    SourceRecipe(kind="sql", sql=sql, description=f"Table {resolved[0]}.{resolved[1]}"),
                    f"{self.settings.pg_database}: {resolved[0]}.{resolved[1]}", sql=result.sql,
                    warnings=result.warnings,
                )
                return record.data, record.label, record.recipe, True
        known = ", ".join(list(self.results)[-8:] + [f"csv:{n}" for n in self.csv_sources]) or "none"
        raise DataSourceError(f"Unknown source '{ref}'. Use a result_id, csv:<name> or an approved "
                              f"schema.table. Known results: {known}")


# ------------------------------------------------------------------ summaries
def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if value is pd.NA or value is pd.NaT:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(value)


def _is_geometry_like(series: pd.Series) -> bool:
    sample = series.dropna().head(20)
    if sample.empty:
        return False
    if sample.map(lambda v: isinstance(v, (bytes, bytearray, memoryview))).any():
        return True
    text = sample.astype(str)
    return bool(text.str.match(r"^(0[0-9]{3}|01)0000[0-9A-Fa-f]{2}").mean() > 0.8
                or text.str.match(r'^\s*\{\s*"type"\s*:\s*"(Multi)?(Point|LineString|Polygon|GeometryCollection)').mean() > 0.8
                or text.str.match(r"^(SRID=\d+;)?(POINT|LINESTRING|POLYGON|MULTI)", case=False).mean() > 0.8
                or str(series.dtype) == "geometry")


def frame_summary(df: pd.DataFrame, max_rows: int, max_cell_chars: int = 80) -> dict[str, Any]:
    """A compact, model-safe description of a data frame.

    Geometry and binary columns are masked; long strings are truncated; at most
    `max_rows` sample rows are included.
    """
    masked = [c for c in df.columns if _is_geometry_like(df[c])]
    sample = df.drop(columns=masked).head(max_rows)
    rows = [
        {str(k): (_jsonable(v)[:max_cell_chars] if isinstance(_jsonable(v), str) else _jsonable(v))
         for k, v in row.items()}
        for row in sample.to_dict(orient="records")
    ]
    numeric_cols = [c for c in df.columns if c not in masked and pd.api.types.is_numeric_dtype(df[c])][:15]
    numeric = {}
    for c in numeric_cols:
        s = pd.to_numeric(df[c], errors="coerce")
        numeric[str(c)] = {k: _jsonable(v) for k, v in
                           {"sum": s.sum(), "mean": s.mean(), "min": s.min(), "max": s.max(),
                            "missing": int(s.isna().sum())}.items()}
    return {
        "row_count": int(len(df)),
        "columns": [{"name": str(c), "dtype": "geometry (masked)" if c in masked else str(df[c].dtype)} for c in df.columns],
        "sample_rows": rows,
        "sample_is_complete": len(df) <= max_rows,
        "numeric_summary": numeric,
    }
