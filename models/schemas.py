"""Pydantic models shared across services, tools, dashboards and the UI."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- SQL / data
class TableRef(BaseModel):
    schema_name: str
    table_name: str

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


class SQLValidationResult(BaseModel):
    ok: bool
    original_sql: str
    safe_sql: str | None = None  # normalised, schema-qualified, row-capped
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    row_cap: int | None = None


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False
    geometry_type: str | None = None
    srid: int | None = None
    description: str | None = None


class ForeignKeyInfo(BaseModel):
    column: str
    references_table: str
    references_column: str


class TableInfo(BaseModel):
    schema_name: str
    table_name: str
    table_type: str
    estimated_rows: int | None = None
    columns: list[ColumnInfo] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    description: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.table_name}"

    @property
    def geometry_columns(self) -> list[ColumnInfo]:
        return [c for c in self.columns if c.geometry_type or c.data_type in ("geometry", "geography")]


# ---------------------------------------------------------------- charts
ChartType = Literal[
    "bar", "line", "area", "scatter", "histogram", "box", "heatmap",
    "correlation", "kpi", "ranked_table", "forecast", "choropleth", "point_map", "proportional_map",
]


class ChartSpec(BaseModel):
    chart_type: ChartType
    title: str
    x: str | None = None
    y: str | None = None
    color: str | None = None
    value: str | None = None  # KPI / heatmap value / map value
    columns: list[str] = Field(default_factory=list)  # correlation / ranked table
    aggregate: Literal["none", "sum", "mean", "count", "median", "min", "max"] = "none"
    x_label: str | None = None
    y_label: str | None = None
    units: str | None = None
    source: str = ""
    filters_text: str = "None"
    date_range: str | None = None
    interpretation: str = ""
    warnings: list[str] = Field(default_factory=list)
    sort_desc: bool = True
    top_n: int | None = Field(default=None, ge=1, le=500)
    # Maps
    geometry: str | None = None  # GeoJSON column (EPSG:4326)
    lat: str | None = None
    lon: str | None = None
    label: str | None = None  # feature name shown on hover
    frame: str | None = None  # animation (time-series map)
    classification: Literal["continuous", "quantile"] = "continuous"
    swap_lat_lon: bool = False


# ---------------------------------------------------------------- dashboards
class SourceRecipe(BaseModel):
    """How to recompute a dashboard component from its original source."""

    kind: Literal["sql", "csv"]
    sql: str | None = None
    csv_name: str | None = None
    csv_snapshot_path: str | None = None
    # Ordered, re-playable analysis steps applied to the base data, e.g.
    # {"method": "time_trend", "params": {...}}. Empty for plain query results.
    transforms: list[dict[str, Any]] = Field(default_factory=list)
    description: str = ""


class FilterSpec(BaseModel):
    id: str = Field(default_factory=lambda: new_id("flt"))
    column: str
    label: str
    kind: Literal["date_range", "multiselect", "numeric_range"]


class DashboardComponent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cmp"))
    title: str
    chart: ChartSpec
    recipe: SourceRecipe
    width: Literal["full", "half"] = "half"
    order: int = 0


class DashboardSpec(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dsh"))
    name: str
    description: str = ""
    components: list[DashboardComponent] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------- history
class HistoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("hst"))
    created_at: datetime = Field(default_factory=utc_now)
    question: str
    sources: list[str] = Field(default_factory=list)
    sql: list[str] = Field(default_factory=list)
    status: Literal["success", "partial", "error", "clarification"] = "success"
    methods: list[str] = Field(default_factory=list)
    charts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- agent
class ToolEvent(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    summary: str = ""
    result_id: str | None = None
    chart_id: str | None = None
    sql: str | None = None
    warnings: list[str] = Field(default_factory=list)
    requires_approval: bool = False
