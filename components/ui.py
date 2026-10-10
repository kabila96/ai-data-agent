"""Reusable Streamlit building blocks: result panels, chart cards, warnings, downloads."""

from __future__ import annotations

import io
import re

import pandas as pd
import streamlit as st

from tools.context import AgentContext, ChartRecord, ResultRecord

MAX_DISPLAY_ROWS = 2000


def warning_box(warnings: list[str], title: str = "Data-quality warnings") -> None:
    unique = list(dict.fromkeys(w for w in warnings if w))
    if not unique:
        return
    with st.expander(f"⚠️ {title} ({len(unique)})", expanded=len(unique) <= 3):
        for w in unique[:30]:
            st.markdown(f"- {w}")


def sql_expander(sql: str, label: str = "Executed SQL") -> None:
    with st.expander(f"🧾 {label}"):
        st.code(sql, language="sql")


def safe_filename(text: str, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60] or "result"
    return f"{stem}.{ext}"


def show_dataframe(df: pd.DataFrame, height: int | None = None) -> None:
    display = df
    from tools.context import _is_geometry_like

    geometry_like = [c for c in df.columns if _is_geometry_like(df[c])]
    if geometry_like:
        display = df.drop(columns=geometry_like)
        st.caption(f"Geometry column(s) hidden from the table: {', '.join(map(str, geometry_like))}")
    if len(display) > MAX_DISPLAY_ROWS:
        st.caption(f"Showing the first {MAX_DISPLAY_ROWS:,} of {len(display):,} rows. Download for the full result.")
        display = display.head(MAX_DISPLAY_ROWS)
    st.dataframe(display, hide_index=True, height=height or "auto")


def download_buttons(df: pd.DataFrame, label: str, key: str) -> None:
    from geospatial.maps import MapError, find_geojson_column, to_geojson_download

    geo_col = find_geojson_column(df)
    c1, c2, c3 = st.columns(3)
    if geo_col:
        try:
            c3.download_button("⬇️ GeoJSON", to_geojson_download(df, geo_col).encode("utf-8"),
                               file_name=safe_filename(label, "geojson"), mime="application/geo+json",
                               key=f"geo_{key}")
        except MapError:
            pass
    flat = df.drop(columns=[geo_col]) if geo_col else df
    c1.download_button("⬇️ CSV", flat.to_csv(index=False).encode("utf-8"), file_name=safe_filename(label, "csv"),
                       mime="text/csv", key=f"csv_{key}")
    buffer = io.BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            flat.head(1_000_000).to_excel(writer, index=False, sheet_name="result")
        c2.download_button("⬇️ Excel", buffer.getvalue(), file_name=safe_filename(label, "xlsx"),
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"xlsx_{key}")
    except Exception:
        c2.caption("Excel export unavailable (xlsxwriter not installed).")


def result_panel(ctx: AgentContext, record: ResultRecord, key: str, allow_download: bool = False) -> None:
    st.markdown(f"**{record.label}** · {len(record.data):,} rows · source: {record.source_label}")
    show_dataframe(record.data, height=min(400, 40 + 35 * len(record.data)))
    if record.sql:
        sql_expander(record.sql)
    if allow_download:
        st.caption("Review the table above before downloading.")
        download_buttons(record.data, record.label, key)


def chart_card(ctx: AgentContext, chart: ChartRecord, key: str) -> None:
    st.plotly_chart(chart.figure, key=f"fig_{key}")
    if chart.spec.interpretation:
        st.markdown(f"_{chart.spec.interpretation}_")
    warning_box(chart.spec.warnings, "Warnings affecting this chart")
    with st.popover("➕ Save to dashboard"):
        names = [d.name for d in ctx.store.list_dashboards()]
        choice = st.selectbox("Dashboard", ["New dashboard…"] + names, key=f"dsel_{key}")
        name = st.text_input("New dashboard name", key=f"dname_{key}") if choice == "New dashboard…" else choice
        title = st.text_input("Component title", value=chart.spec.title, key=f"dtitle_{key}")
        width = st.radio("Width", ["half", "full"], horizontal=True, key=f"dwidth_{key}")
        if st.button("Save", key=f"dsave_{key}", type="primary", disabled=not name):
            from dashboards.service import add_component_from_chart
            from security.errors import AppError

            try:
                spec, _ = add_component_from_chart(ctx, chart, name, title, width)
                st.success(f"Saved to '{spec.name}' ({len(spec.components)} components).")
            except AppError as exc:
                st.error(exc.user_message)


def connection_badge() -> None:
    from components.state import get_services

    services = get_services()
    if services.database_ready:
        role = services.security.role if services.security else "?"
        postgis = services.security.postgis_version if services.security else None
        st.sidebar.success(
            f"Database: {services.settings.pg_database}\n\nRole: {role} (read-only)"
            + (f"\n\nPostGIS {postgis}" if postgis else "")
        )
    else:
        st.sidebar.error(f"Database not connected.\n\n{services.db_error or ''}")
    if not services.settings.openai_configured:
        st.sidebar.warning("OpenAI API key not set; the chat agent is disabled.")
