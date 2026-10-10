"""Dashboard Builder: open, filter, arrange, refresh and export saved dashboards."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from components.branding import page_header

from components.state import get_context
from components.ui import warning_box
from dashboards.service import (
    FilterValue, describe_filters, export_html, export_spec_json, export_zip, move_component,
    remove_component, render_component,
)
from models.schemas import FilterSpec

ctx = get_context()
page_header("Dashboards", "Saved charts that re-run their queries when you refresh them. Add filters, rearrange, export.")

dashboards = ctx.store.list_dashboards()
if not dashboards:
    st.info("No dashboards yet. Use **➕ Save to dashboard** under any chart in Chat or the Visualisation Workspace, "
            "or ask the agent to “save this chart to a dashboard called Sales overview”.")
    st.stop()

ids = [d.id for d in dashboards]
names = {d.id: f"{d.name} ({len(d.components)} components)" for d in dashboards}
chosen = st.selectbox("Dashboard", ids, format_func=names.get,
                      index=ids.index(st.session_state["open_dashboard"]) if st.session_state.get("open_dashboard") in ids else 0)
st.session_state.open_dashboard = chosen
spec = ctx.store.get_dashboard(chosen)

edit_mode = st.toggle("Edit layout and filters", value=False)

# ---------------------------------------------------------------- editing
if edit_mode:
    with st.container(border=True):
        c1, c2 = st.columns(2)
        new_name = c1.text_input("Dashboard name", spec.name)
        new_desc = c2.text_input("Description", spec.description)
        st.markdown("**Components**")
        for comp in sorted(spec.components, key=lambda c: c.order):
            a, b, c, d, e, f = st.columns([4, 1.4, 0.6, 0.6, 0.8, 0.6])
            comp.title = a.text_input("Title", comp.title, key=f"t_{comp.id}", label_visibility="collapsed")
            comp.width = b.selectbox("Width", ["half", "full"], index=["half", "full"].index(comp.width),
                                     key=f"w_{comp.id}", label_visibility="collapsed")
            if c.button("⬆", key=f"up_{comp.id}"):
                move_component(spec, comp.id, -1)
                ctx.store.save_dashboard(spec)
                st.rerun()
            if d.button("⬇", key=f"dn_{comp.id}"):
                move_component(spec, comp.id, 1)
                ctx.store.save_dashboard(spec)
                st.rerun()
            e.caption(comp.chart.chart_type)
            if f.button("🗑", key=f"rm_{comp.id}"):
                remove_component(spec, comp.id)
                ctx.store.save_dashboard(spec)
                st.rerun()
        st.markdown("**Filters** (apply to every component whose data has the column)")
        for flt in list(spec.filters):
            x1, x2, x3 = st.columns([3, 2, 1])
            x1.write(f"{flt.label} → `{flt.column}`")
            x2.write(flt.kind)
            if x3.button("Remove", key=f"rmf_{flt.id}"):
                spec.filters = [f for f in spec.filters if f.id != flt.id]
                ctx.store.save_dashboard(spec)
                st.rerun()
        f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
        col = f1.text_input("Column name", placeholder="e.g. sale_date or branch_name")
        lbl = f2.text_input("Label", placeholder="e.g. Date")
        kind = f3.selectbox("Type", ["date_range", "multiselect", "numeric_range"])
        if f4.button("Add filter", disabled=not col):
            spec.filters.append(FilterSpec(column=col.strip(), label=(lbl or col).strip(), kind=kind))
            ctx.store.save_dashboard(spec)
            st.rerun()
        s1, s2 = st.columns(2)
        if s1.button("💾 Save changes", type="primary"):
            spec.name, spec.description = new_name.strip() or spec.name, new_desc
            ctx.store.save_dashboard(spec)
            st.success("Saved.")
        with s2.popover("Delete dashboard"):
            st.write("This removes the saved configuration (no data is stored).")
            if st.button("Confirm delete", type="primary"):
                ctx.store.delete_dashboard(spec.id)
                st.session_state.pop("open_dashboard", None)
                st.rerun()

# ---------------------------------------------------------------- filters
st.subheader(spec.name)
if spec.description:
    st.caption(spec.description)

filter_values: list[FilterValue] = []
if spec.filters:
    cols = st.columns(min(4, len(spec.filters)))
    for i, flt in enumerate(spec.filters):
        with cols[i % len(cols)]:
            if flt.kind == "date_range":
                picked = st.date_input(flt.label, value=(), key=f"fv_{flt.id}")
                if isinstance(picked, (list, tuple)) and len(picked) == 2:
                    filter_values.append(FilterValue(flt, (picked[0], picked[1])))
            elif flt.kind == "numeric_range":
                lo = st.number_input(f"{flt.label} min", value=None, key=f"lo_{flt.id}")
                hi = st.number_input(f"{flt.label} max", value=None, key=f"hi_{flt.id}")
                if lo is not None or hi is not None:
                    filter_values.append(FilterValue(flt, (lo, hi)))
            else:
                raw = st.text_input(f"{flt.label} (comma-separated values)", key=f"fv_{flt.id}")
                values = [v.strip() for v in raw.split(",") if v.strip()]
                if values:
                    filter_values.append(FilterValue(flt, values))

refresh = st.button("🔄 Refresh from sources", type="primary")
cache_key = f"dash_render_{spec.id}"
signature = (spec.model_dump_json(), describe_filters(filter_values))
if refresh or st.session_state.get(cache_key, (None, None))[0] != signature:
    with st.spinner("Re-running component queries…"):
        rendered = [render_component(ctx, c, filter_values) for c in sorted(spec.components, key=lambda c: c.order)]
    st.session_state[cache_key] = (signature, rendered)
rendered = st.session_state[cache_key][1]

st.caption(f"Filters: {describe_filters(filter_values)} · refreshed {date.today():%d %b %Y}")

row: list = []
for item in rendered:
    if item.component.width == "full":
        row.append([item])
    elif row and len(row[-1]) == 1 and row[-1][0].component.width == "half":
        row[-1].append(item)
    else:
        row.append([item])
for group in row:
    columns = st.columns(len(group)) if group[0].component.width == "half" else [st.container()]
    for col, item in zip(columns, group):
        with col:
            with st.container(border=True):
                if item.figure is not None:
                    st.plotly_chart(item.figure, key=f"dfig_{item.component.id}")
                    if item.component.chart.interpretation:
                        st.caption(item.component.chart.interpretation)
                    warning_box(item.warnings + item.component.chart.warnings, "Warnings")
                else:
                    st.markdown(f"**{item.component.title}**")
                    st.error(item.error or "Could not render this component.")

st.divider()
e1, e2, e3 = st.columns(3)
filters_text = describe_filters(filter_values)
e1.download_button("⬇️ Data (ZIP of CSVs)", export_zip(spec, rendered), f"{spec.name}_data.zip", "application/zip")
e2.download_button("⬇️ Printable summary (HTML)", export_html(spec, rendered, filters_text).encode("utf-8"),
                   f"{spec.name}_summary.html", "text/html",
                   help="Open in a browser and use Print → Save as PDF.")
e3.download_button("⬇️ Configuration (JSON)", export_spec_json(spec).encode("utf-8"), f"{spec.name}.json",
                   "application/json")
