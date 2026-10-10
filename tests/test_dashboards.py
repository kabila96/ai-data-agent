"""Dashboard persistence, refresh, filters and export."""

from __future__ import annotations

import io
import zipfile
from datetime import date

from analysis.csv_loader import load_csv
from dashboards.service import (
    FilterValue, add_component_from_chart, build_where, export_html, export_zip, move_component, render_component,
)
from models.schemas import ChartSpec, DashboardSpec, FilterSpec
from storage.app_store import AppStore
from tests.conftest import requires_db
from tools import registry
from tools.context import CSVSource


def _load(ctx, sample_csv_bytes):
    loaded = load_csv("branch_sales_upload.csv", sample_csv_bytes)
    ctx.csv_sources["branch_sales_upload.csv"] = CSVSource(
        "branch_sales_upload.csv", loaded.raw, loaded.data, loaded.inferred_types, loaded.warnings)


def test_csv_dashboard_saved_reopened_and_refreshed(offline_ctx, sample_csv_bytes, tmp_settings):
    ctx = offline_ctx
    _load(ctx, sample_csv_bytes)
    out = registry.dispatch(ctx, "run_statistical_analysis", {
        "source": "csv:branch_sales_upload.csv", "method": "group_summary",
        "params": {"group": "category", "value": "revenue_gbp"}})
    chart_out = registry.dispatch(ctx, "generate_visualisation", {
        "source": out.result_id, "title": "Revenue by category", "chart_type": "bar",
        "x": "category", "y": "total", "units": "£"})
    assert chart_out.ok, chart_out.payload
    save = registry.dispatch(ctx, "save_dashboard", {"dashboard_name": "Sales overview", "chart_id": chart_out.chart_id})
    assert save.ok, save.payload

    # Simulate an application restart: new store object, CSV no longer in session.
    store2 = AppStore(tmp_settings.app_data_dir / "store.sqlite3", json_dir=tmp_settings.dashboards_dir)
    spec = store2.find_dashboard_by_name("sales OVERVIEW")
    assert spec is not None and len(spec.components) == 1
    assert (tmp_settings.dashboards_dir / f"{spec.id}.json").exists()
    comp = spec.components[0]
    assert comp.recipe.csv_snapshot_path and comp.recipe.transforms[0]["method"] == "group_summary"

    ctx.csv_sources.clear()
    rendered = render_component(ctx, comp, [])
    assert rendered.error is None, rendered.error
    assert rendered.figure is not None and len(rendered.data) == 6
    assert any("copy" in w for w in rendered.warnings)

    # Filters are applied to the base data before the analysis is replayed.
    flt = FilterSpec(column="branch", label="Branch", kind="multiselect")
    filtered = render_component(ctx, comp, [FilterValue(flt, ["Digbeth"])])
    assert filtered.data["total"].sum() < rendered.data["total"].sum()

    data_zip = zipfile.ZipFile(io.BytesIO(export_zip(spec, [rendered])))
    assert "dashboard.json" in data_zip.namelist() and len(data_zip.namelist()) == 2
    html = export_html(spec, [rendered], "None")
    assert "Revenue by category" in html and "<script" in html


def test_build_where_uses_bound_parameters_only():
    flt_d = FilterSpec(column='sale"date', label="Date", kind="date_range")
    flt_m = FilterSpec(column="branch", label="Branch", kind="multiselect")
    where, params, skipped = build_where(
        [FilterValue(flt_d, (date(2026, 1, 1), None)), FilterValue(flt_m, ["x'; DROP TABLE t; --"]),
         FilterValue(FilterSpec(column="nope", label="Nope", kind="multiselect"), ["a"])],
        {'sale"date', "branch"},
    )
    assert where == '"sale""date" >= :f0_start AND "branch" IN :f1_values'
    assert params["f1_values"] == ["x'; DROP TABLE t; --"]
    assert skipped == ["Nope"]


def test_move_component_reorders():
    spec = DashboardSpec(name="d")
    from models.schemas import DashboardComponent, SourceRecipe

    for i in range(3):
        spec.components.append(DashboardComponent(title=str(i), order=i, chart=ChartSpec(chart_type="kpi", title=str(i)),
                                                  recipe=SourceRecipe(kind="csv", csv_name="x")))
    move_component(spec, spec.components[2].id, -2)
    assert [c.title for c in sorted(spec.components, key=lambda c: c.order)] == ["2", "0", "1"]


@requires_db
def test_sql_dashboard_refresh_with_filters(live_ctx, demo_sales):
    out = registry.dispatch(live_ctx, "execute_read_only_sql", {
        "sql": "SELECT s.sale_date, b.branch_name, s.revenue FROM training.demo_sales s "
               "JOIN training.demo_branches b USING (branch_id) WHERE s.sale_date >= date '2026-07-01'",
        "purpose": "recent sales"})
    assert out.ok, out.payload
    chart = registry.dispatch(live_ctx, "generate_visualisation", {
        "source": out.result_id, "title": "Revenue by branch", "chart_type": "bar",
        "x": "branch_name", "y": "revenue", "aggregate": "sum", "units": "£"})
    save = registry.dispatch(live_ctx, "save_dashboard", {"dashboard_name": "Live", "chart_id": chart.chart_id})
    assert save.ok, save.payload
    spec = live_ctx.store.find_dashboard_by_name("Live")
    flt = FilterSpec(column="branch_name", label="Branch", kind="multiselect")
    rendered = render_component(live_ctx, spec.components[0], [FilterValue(flt, ["Camden"])])
    assert rendered.error is None, rendered.error
    assert set(rendered.data["branch_name"]) == {"Camden"}
    assert "Branch in Camden" in rendered.figure.layout.annotations[-1].text


def test_map_component_saved_and_refreshed(offline_ctx, tmp_settings):
    import json as _json

    from analysis.csv_loader import load_csv
    from dashboards.service import render_component
    from tools.context import CSVSource

    rows = []
    for i in range(4):
        ring = [[-1.9 + i * 0.01, 52.47], [-1.89 + i * 0.01, 52.47], [-1.89 + i * 0.01, 52.48], [-1.9 + i * 0.01, 52.47]]
        rows.append({"name": f"A{i}", "value": i + 1, "geojson": _json.dumps({"type": "Polygon", "coordinates": [ring]})})
    import pandas as _pd

    loaded = load_csv("areas.csv", _pd.DataFrame(rows).to_csv(index=False).encode())
    offline_ctx.csv_sources["areas.csv"] = CSVSource("areas.csv", loaded.raw, loaded.data, loaded.inferred_types)
    m = registry.dispatch(offline_ctx, "generate_geospatial_map", {"source": "csv:areas.csv", "map_type": "choropleth",
                                                                   "title": "Value by area", "value": "value"})
    assert m.ok, m.payload
    assert registry.dispatch(offline_ctx, "save_dashboard", {"dashboard_name": "Maps", "chart_id": m.chart_id}).ok
    spec = offline_ctx.store.find_dashboard_by_name("Maps")
    rendered = render_component(offline_ctx, spec.components[0], [])
    assert rendered.error is None, rendered.error
    assert rendered.figure.data[0].type == "choroplethmap"
