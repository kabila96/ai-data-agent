"""Visualisation Workspace: build and refine charts from any result or CSV."""

from __future__ import annotations

import streamlit as st

from components.branding import page_header

from components.state import get_context
from components.ui import chart_card
from models.schemas import ChartSpec, SourceRecipe
from tools.context import ChartRecord
from geospatial.maps import find_geojson_column
from visualisations.chart_selector import recommend_chart
from visualisations.charts import SUPPORTED_CHARTS, ChartError, build_figure

ctx = get_context()
page_header("Visualisation workspace", "Build or refine a chart from any result or uploaded file, then save it to a dashboard.")

sources: dict[str, str] = {}
for rid, rec in reversed(list(ctx.results.items())):
    sources[rid] = f"{rec.label} · {len(rec.data):,} rows"
for name in ctx.csv_sources:
    sources[f"csv:{name}"] = f"CSV: {name}"
if not sources:
    st.info("No data yet. Ask a question in Chat, preview a table in the Explorer, or upload a CSV.")
    st.stop()

keys = list(sources)
default = st.session_state.get("viz_source")
source = st.selectbox("Data", keys, index=keys.index(default) if default in keys else 0, format_func=sources.get)
if source.startswith("csv:"):
    data, label = ctx.csv_sources[source[4:]].data, f"CSV {source[4:]}"
else:
    data, label = ctx.results[source].data, ctx.results[source].label

columns = ["(none)"] + [str(c) for c in data.columns]
recommendation = recommend_chart(data, st.session_state.get("viz_question", ""))
st.caption(f"Suggested: **{recommendation.chart_type or 'table only'}**: {recommendation.reason}")

c1, c2 = st.columns([1, 2])
with c1:
    chart_types = list(SUPPORTED_CHARTS) + ["choropleth", "point_map", "proportional_map"]
    chart_type = st.selectbox("Chart type", chart_types,
                              index=chart_types.index(recommendation.chart_type) if recommendation.chart_type in chart_types else 0)

    def pick(label_text: str, suggested: str | None, key: str) -> str | None:
        idx = columns.index(suggested) if suggested in columns else 0
        value = st.selectbox(label_text, columns, index=idx, key=key)
        return None if value == "(none)" else value

    x = pick("X / category / date", recommendation.x, "viz_x")
    y = pick("Y / measure", recommendation.y, "viz_y")
    color = pick("Colour / series (optional)", recommendation.color, "viz_color")
    value = pick("Value (KPI, heat map)", recommendation.value or recommendation.y, "viz_value")
    multi = st.multiselect("Columns (correlation / ranked table)", [str(c) for c in data.columns],
                           default=recommendation.columns or [])
    aggregate = st.selectbox("Aggregate duplicates", ["none", "sum", "mean", "count", "median", "min", "max"])
    top_n = st.number_input("Top N (0 = all)", 0, 500, 0)
    measure_name = (y or value or "Value").replace("_", " ").capitalize()
    default_title = f"{measure_name} by {x.replace('_', ' ')}" if x else measure_name
    title = st.text_input("Title", value=default_title)
    units = st.text_input("Units (e.g. £, facilities, km²)")
    filters_text = st.text_input("Filters applied", value="None")
    date_range = st.text_input("Date range (if relevant)")
    interpretation = st.text_area("Interpretation (shown under the chart)")
    geometry = lat = lon = feature_label = None
    classification = "continuous"
    if chart_type in ("choropleth", "point_map", "proportional_map"):
        st.markdown("**Map settings**")
        geometry = pick("GeoJSON geometry column (EPSG:4326)", find_geojson_column(data), "viz_geom")
        lat = pick("Latitude column (if no geometry)", next((c for c in columns if c.lower() in ("lat", "latitude", "y")), None), "viz_lat")
        lon = pick("Longitude column (if no geometry)", next((c for c in columns if c.lower() in ("lon", "lng", "longitude", "x")), None), "viz_lon")
        feature_label = pick("Name shown on hover", None, "viz_label")
        if chart_type == "choropleth":
            classification = st.radio("Colours", ["continuous", "quantile"], horizontal=True)
with c2:
    record = ctx.results.get(source)
    spec = ChartSpec(
        chart_type=chart_type, title=title or "Untitled chart", x=x, y=y, color=color, value=value, columns=multi,
        aggregate=aggregate, units=units or None, source=record.source_label if record else label,
        filters_text=filters_text or "None", date_range=date_range or None, interpretation=interpretation,
        warnings=record.warnings[:5] if record else [], top_n=int(top_n) or None,
        geometry=geometry, lat=lat, lon=lon, label=feature_label, classification=classification,
    )
    try:
        figure = build_figure(spec, data)
    except ChartError as exc:
        st.warning(f"Cannot draw this chart: {exc}")
        st.stop()
    if record is None:  # a CSV: register it once as a result so the chart has a refreshable recipe
        record = ctx.add_result(data, label, SourceRecipe(kind="csv", csv_name=source[4:]), label)
        st.session_state.viz_source = record.id
    chart = ChartRecord("cht_workspace", spec, figure, record.id)
    ctx.charts[chart.id] = chart  # overwritten on every edit, so the session does not accumulate charts
    chart_card(ctx, chart, key="workspace")
