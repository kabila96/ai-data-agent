"""Dashboard persistence, refresh, filters and export.

A dashboard stores *recipes*, not data: each component keeps the validated
source query (or CSV reference), the analysis steps applied to it and the chart
specification. Refreshing re-runs the recipe through the same SQL validator and
read-only executor used by the agent.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from analysis.transforms import replay
from models.schemas import DashboardComponent, DashboardSpec, FilterSpec, SourceRecipe
from security.errors import DataSourceError
from security.sql_validator import quote_ident
from tools.context import AgentContext, ChartRecord
from visualisations.charts import ChartError, build_figure


# --------------------------------------------------------------- saving
def _snapshot_csv(ctx: AgentContext, csv_name: str) -> str:
    src = ctx.csv_sources[csv_name]
    folder = ctx.settings.dashboards_dir / "data"
    folder.mkdir(parents=True, exist_ok=True)
    csv_bytes = src.raw.to_csv(index=False).encode("utf-8")
    digest = hashlib.sha256(csv_bytes).hexdigest()[:12]
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in csv_name)[:60]
    path = folder / f"{Path(safe).stem}_{digest}.csv"
    if not path.exists():
        path.write_bytes(csv_bytes)
    return str(path.relative_to(ctx.settings.dashboards_dir))


def add_component_from_chart(
    ctx: AgentContext, chart: ChartRecord, dashboard_name: str,
    title: str | None = None, width: str = "half",
) -> tuple[DashboardSpec, DashboardComponent]:
    record = ctx.results.get(chart.result_id)
    if record is None:
        raise DataSourceError("The chart's data is no longer in this session; recreate the chart and save again.")
    if not record.refreshable:
        raise DataSourceError(
            "This result comes from a joined dataset, which cannot yet be refreshed automatically. "
            "Express the join in SQL (for database tables) or save the inputs' charts separately."
        )
    recipe = record.recipe.model_copy()
    if recipe.kind == "csv" and recipe.csv_name in ctx.csv_sources:
        recipe.csv_snapshot_path = _snapshot_csv(ctx, recipe.csv_name)
    spec = ctx.store.find_dashboard_by_name(dashboard_name) or DashboardSpec(name=dashboard_name.strip())
    component = DashboardComponent(
        title=title or chart.spec.title, chart=chart.spec, recipe=recipe,
        width=width if width in ("full", "half") else "half", order=len(spec.components),
    )
    spec.components.append(component)
    spec = ctx.store.save_dashboard(spec)
    return spec, component


# --------------------------------------------------------------- filters
@dataclass
class FilterValue:
    spec: FilterSpec
    value: Any  # (start, end) for ranges, list for multiselect


def build_where(filters: list[FilterValue], available_columns: set[str]) -> tuple[str, dict[str, Any], list[str]]:
    """Build a WHERE clause from quoted identifiers and bound parameters only."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    skipped: list[str] = []
    for i, f in enumerate(filters):
        col = f.spec.column
        if col not in available_columns:
            skipped.append(f.spec.label)
            continue
        if f.value in (None, [], ()):
            continue
        ident = quote_ident(col)
        if f.spec.kind in ("date_range", "numeric_range"):
            start, end = f.value
            if start is not None:
                params[f"f{i}_start"] = start
                clauses.append(f"{ident} >= :f{i}_start")
            if end is not None:
                params[f"f{i}_end"] = end
                clauses.append(f"{ident} <= :f{i}_end")
        else:
            params[f"f{i}_values"] = list(f.value)
            clauses.append(f"{ident} IN :f{i}_values")
    return " AND ".join(clauses), params, skipped


def apply_frame_filters(df: pd.DataFrame, filters: list[FilterValue]) -> tuple[pd.DataFrame, list[str]]:
    skipped: list[str] = []
    for f in filters:
        col = f.spec.column
        if col not in df.columns:
            skipped.append(f.spec.label)
            continue
        if f.value in (None, [], ()):
            continue
        if f.spec.kind == "date_range":
            series = pd.to_datetime(df[col], errors="coerce")
            start, end = f.value
            if start is not None:
                df = df[series >= pd.Timestamp(start)]
                series = series.loc[df.index]
            if end is not None:
                df = df[series <= pd.Timestamp(end)]
        elif f.spec.kind == "numeric_range":
            series = pd.to_numeric(df[col], errors="coerce")
            start, end = f.value
            df = df[(series >= start) & (series <= end)]
        else:
            df = df[df[col].astype(str).isin([str(v) for v in f.value])]
    return df, skipped


def describe_filters(filters: list[FilterValue]) -> str:
    parts = []
    for f in filters:
        if f.value in (None, [], ()):
            continue
        if isinstance(f.value, (list, set)) and f.spec.kind == "multiselect":
            values = list(f.value)
            parts.append(f"{f.spec.label} in {', '.join(map(str, values[:5]))}{'…' if len(values) > 5 else ''}")
        else:
            start, end = f.value
            parts.append(f"{f.spec.label} {_fmt(start)} to {_fmt(end)}")
    return "; ".join(parts) or "None"


def _fmt(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return "…" if value is None else str(value)


# --------------------------------------------------------------- refresh
@dataclass
class RenderedComponent:
    component: DashboardComponent
    data: pd.DataFrame | None
    figure: go.Figure | None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def load_base_data(ctx: AgentContext, recipe: SourceRecipe, filters: list[FilterValue]) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    if recipe.kind == "sql":
        executor = ctx.executor
        if executor is None:
            raise DataSourceError("The database is not connected.")
        if filters:
            probe = executor.run(recipe.sql or "", max_rows=1, source="dashboard")
            where, params, skipped = build_where(filters, set(probe.data.columns))
            warnings += [f"Filter '{s}' does not apply to this component." for s in skipped]
            result = executor.run_wrapped(recipe.sql or "", where, params, source="dashboard")
        else:
            result = executor.run(recipe.sql or "", source="dashboard")
        return result.data, warnings + result.warnings

    name = recipe.csv_name or ""
    if name in ctx.csv_sources:
        df = ctx.csv_sources[name].data
    elif recipe.csv_snapshot_path:
        from analysis.csv_loader import load_csv

        path = (ctx.settings.dashboards_dir / recipe.csv_snapshot_path).resolve()
        if not str(path).startswith(str(ctx.settings.dashboards_dir.resolve())) or not path.exists():
            raise DataSourceError(f"Saved copy of '{name}' is missing; upload the CSV again.")
        df = load_csv(name, path.read_bytes()).data
        warnings.append(f"Using the copy of '{name}' saved with the dashboard; upload a newer file to refresh it.")
    else:
        raise DataSourceError(f"CSV '{name}' is not loaded. Upload it on the CSV Analysis page.")
    df, skipped = apply_frame_filters(df, filters)
    warnings += [f"Filter '{s}' does not apply to this component." for s in skipped]
    return df, warnings


def render_component(ctx: AgentContext, component: DashboardComponent, filters: list[FilterValue]) -> RenderedComponent:
    try:
        base, warnings = load_base_data(ctx, component.recipe, filters)
        data = replay(base, component.recipe.transforms)
        spec = component.chart.model_copy(update={
            "title": component.title,
            "filters_text": describe_filters(filters) if filters else component.chart.filters_text,
        })
        figure = build_figure(spec, data)
        return RenderedComponent(component, data, figure, None, warnings)
    except (ChartError, DataSourceError, KeyError, ValueError) as exc:
        message = getattr(exc, "user_message", None) or str(exc)
        return RenderedComponent(component, None, None, message)
    except Exception as exc:  # pragma: no cover - defensive
        from security.errors import to_user_message

        return RenderedComponent(component, None, None, to_user_message(exc, "dashboard refresh"))


# --------------------------------------------------------------- editing
def move_component(spec: DashboardSpec, component_id: str, delta: int) -> DashboardSpec:
    items = sorted(spec.components, key=lambda c: c.order)
    idx = next(i for i, c in enumerate(items) if c.id == component_id)
    new = max(0, min(len(items) - 1, idx + delta))
    items.insert(new, items.pop(idx))
    for i, c in enumerate(items):
        c.order = i
    spec.components = items
    return spec


def remove_component(spec: DashboardSpec, component_id: str) -> DashboardSpec:
    spec.components = [c for c in spec.components if c.id != component_id]
    for i, c in enumerate(sorted(spec.components, key=lambda c: c.order)):
        c.order = i
    return spec


# --------------------------------------------------------------- export
def export_zip(spec: DashboardSpec, rendered: list[RenderedComponent]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dashboard.json", spec.model_dump_json(indent=2))
        for i, r in enumerate(rendered, 1):
            if r.data is not None:
                safe = "".join(ch if ch.isalnum() else "_" for ch in r.component.title)[:50]
                zf.writestr(f"{i:02d}_{safe}.csv", r.data.to_csv(index=False))
    return buffer.getvalue()


def export_html(spec: DashboardSpec, rendered: list[RenderedComponent], filters_text: str) -> str:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(spec.name)}</title>",
        "<script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script>",
        "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#0b0b0b;background:#fcfcfb}"
        ".card{border:1px solid #e6e5e1;border-radius:8px;padding:12px;margin:12px 0;break-inside:avoid;background:#fff}"
        ".muted{color:#52514e;font-size:13px}.warn{color:#8a4b00;font-size:13px}"
        "@media print{.card{page-break-inside:avoid}}</style></head><body>",
        f"<h1>{html.escape(spec.name)}</h1>",
        f"<p class='muted'>Generated {datetime.now():%Y-%m-%d %H:%M}. Filters: {html.escape(filters_text)}</p>",
    ]
    if spec.description:
        parts.append(f"<p>{html.escape(spec.description)}</p>")
    for r in rendered:
        parts.append("<div class='card'>")
        if r.figure is not None:
            parts.append(r.figure.to_html(full_html=False, include_plotlyjs=False))
        else:
            parts.append(f"<h3>{html.escape(r.component.title)}</h3><p class='warn'>Not available: {html.escape(r.error or '')}</p>")
        if r.component.chart.interpretation:
            parts.append(f"<p>{html.escape(r.component.chart.interpretation)}</p>")
        for w in r.warnings + r.component.chart.warnings:
            parts.append(f"<p class='warn'>⚠ {html.escape(w)}</p>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)


def export_spec_json(spec: DashboardSpec) -> str:
    return json.dumps(json.loads(spec.model_dump_json()), indent=2)
