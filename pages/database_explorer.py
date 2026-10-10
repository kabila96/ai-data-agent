"""Database Explorer: approved schemas, tables, columns, geometry and the data dictionary."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.branding import page_header, status_chips

from components.state import get_context, get_services, reconnect
from components.ui import result_panel, show_dataframe
from models.schemas import SourceRecipe
from security.errors import AppError, to_user_message
from security.sql_validator import quote_ident

services = get_services()
ctx = get_context()
page_header("Database explorer", "Approved schemas, tables, columns and geometry. Describe your columns here so the agent uses your definitions.", status_chips(services))

with st.expander("Connection and security status", expanded=not services.database_ready):
    s = services.settings
    st.markdown(
        f"- Host: `{s.pg_host}:{s.pg_port}` · Database: `{s.pg_database}` · Role: `{s.pg_user}`\n"
        f"- Approved schemas (from .env): `{', '.join(s.approved_schemas)}`\n"
        f"- Row limit {s.max_rows:,} · Statement timeout {s.statement_timeout_ms / 1000:.0f}s"
    )
    if services.security:
        r = services.security
        st.markdown(
            f"- Server PostgreSQL {r.server_version.split()[0]} · PostGIS {r.postgis_version or 'not installed'}\n"
            f"- Superuser: {'❌ YES' if r.is_superuser else '✅ no'} · Read-only session: "
            f"{'✅ yes' if r.session_read_only else '❌ NO'} · Write grants: "
            f"{'❌ ' + str(len(r.writable_tables)) if r.writable_tables else '✅ none'}"
        )
    if services.db_error:
        st.error(services.db_error)
    if st.button("🔄 Reconnect / refresh metadata"):
        reconnect()
        if services.metadata:
            services.metadata.clear_cache()
        st.rerun()

if not services.database_ready or services.metadata is None:
    st.stop()

metadata = services.metadata
schemas = metadata.list_schemas()
if not schemas:
    st.warning("No approved schemas are readable by this role.")
    st.stop()

tab_browse, tab_search, tab_dictionary = st.tabs(["Browse", "Search", "Data dictionary"])

with tab_browse:
    left, right = st.columns([1, 2.4])
    with left:
        schema = st.selectbox("Schema", schemas)
        tables = metadata.list_tables(schema)
        if not tables:
            st.info("No readable tables or views in this schema.")
            st.stop()
        labels = {t.table_name: f"{t.table_name}  ·  {t.table_type}" for t in tables}
        table_name = st.radio("Table or view", list(labels), format_func=labels.get, label_visibility="collapsed")
    with right:
        try:
            info = metadata.describe_table(schema, table_name)
        except Exception as exc:
            st.error(to_user_message(exc, "table inspection"))
            st.stop()
        if info is None:
            st.error("Table not found or not approved.")
            st.stop()
        st.subheader(info.qualified)
        dictionary = {d["column_name"]: d for d in ctx.store.get_dictionary(info.schema_name, info.table_name)}
        table_desc = (dictionary.get("") or {}).get("description") or info.description
        st.caption(table_desc or "No description yet. Add one in the Data dictionary tab.")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Type", info.table_type)
        m2.metric("Est. rows", f"{info.estimated_rows:,}" if info.estimated_rows is not None else "n/a")
        m3.metric("Columns", len(info.columns))
        m4.metric("Geometry cols", len(info.geometry_columns))
        cols_df = pd.DataFrame([
            {
                "column": c.name, "type": c.data_type, "nullable": c.nullable, "PK": "🔑" if c.is_primary_key else "",
                "geometry": c.geometry_type or "", "SRID": c.srid if c.srid is not None else "",
                "description": (dictionary.get(c.name) or {}).get("description") or c.description or "",
                "units": (dictionary.get(c.name) or {}).get("units") or "",
            }
            for c in info.columns
        ])
        show_dataframe(cols_df)
        for g in info.geometry_columns:
            if g.srid in (None, 0):
                st.warning(f"Geometry column '{g.name}' has no SRID; its coordinate reference system is unknown.")
            elif g.srid == 4326:
                st.info(f"'{g.name}' is in EPSG:4326 (degrees). Distances/areas must be computed after "
                        f"transforming (EPSG:{ctx.settings.british_crs_epsg} for British data) or as geography.")
        if info.foreign_keys:
            st.markdown("**Foreign keys**")
            show_dataframe(pd.DataFrame([fk.model_dump() for fk in info.foreign_keys]))

        n = st.slider("Preview rows", 5, 100, 20, step=5)
        if st.button("👀 Preview rows", type="primary"):
            sql = f"SELECT * FROM {quote_ident(info.schema_name)}.{quote_ident(info.table_name)} LIMIT {int(n)}"
            try:
                result = services.executor.run(sql, source="explorer")
                record = ctx.add_result(result.data, f"Preview of {info.qualified}",
                                        SourceRecipe(kind="sql", sql=sql, description=f"Preview {info.qualified}"),
                                        f"{ctx.settings.pg_database}.{info.qualified}", sql=result.sql)
                st.session_state.explorer_preview = record.id
            except AppError as exc:
                st.error(exc.user_message)
        rid = st.session_state.get("explorer_preview")
        if rid and rid in ctx.results and ctx.results[rid].label == f"Preview of {info.qualified}":
            result_panel(ctx, ctx.results[rid], key=f"prev_{rid}", allow_download=True)

with tab_search:
    term = st.text_input("Search table and column names", placeholder="e.g. ward, revenue, geom")
    if term:
        hits = metadata.search(term)
        st.caption(f"{len(hits)} match(es)" + (" (first 300)" if len(hits) == 300 else ""))
        if hits:
            show_dataframe(pd.DataFrame(hits))

with tab_dictionary:
    st.markdown(
        "Descriptions and units written here are stored locally and given to the agent with table metadata, "
        "so it uses your definitions (e.g. *revenue excludes VAT*)."
    )
    d_schema = st.selectbox("Schema ", schemas, key="dict_schema")
    d_tables = [t.table_name for t in metadata.list_tables(d_schema)]
    if d_tables:
        d_table = st.selectbox("Table", d_tables, key="dict_table")
        d_info = metadata.describe_table(d_schema, d_table)
        existing = {d["column_name"]: d for d in ctx.store.get_dictionary(d_schema, d_table)}
        rows = [{"column": "(table)", "type": "", "description": (existing.get("") or {}).get("description", ""),
                 "units": ""}]
        rows += [{"column": c.name, "type": c.data_type,
                  "description": (existing.get(c.name) or {}).get("description", ""),
                  "units": (existing.get(c.name) or {}).get("units") or ""} for c in d_info.columns]
        edited = st.data_editor(
            pd.DataFrame(rows), hide_index=True, disabled=["column", "type"], key=f"dict_{d_schema}_{d_table}",
            column_config={"description": st.column_config.TextColumn(width="large", max_chars=2000),
                           "units": st.column_config.TextColumn(width="small", max_chars=40)},
        )
        if st.button("💾 Save descriptions", type="primary"):
            for row in edited.to_dict(orient="records"):
                column = "" if row["column"] == "(table)" else row["column"]
                ctx.store.set_description(d_schema, d_table, column, str(row.get("description") or ""),
                                          str(row.get("units") or "") or None)
            st.success("Data dictionary saved.")
        all_entries = ctx.store.get_dictionary()
        if all_entries:
            st.download_button("⬇️ Export full data dictionary (CSV)",
                               pd.DataFrame(all_entries).to_csv(index=False).encode("utf-8"),
                               "data_dictionary.csv", "text/csv")
