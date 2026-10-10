"""CSV Analysis: upload, type review, quality profile, statistics, comparison and joins."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.branding import page_header

from analysis.csv_loader import TYPE_CHOICES, apply_types, load_csv
from analysis.joins import compare_frames, join_frames, join_report, suggest_join_keys
from analysis.profiling import duplicate_examples, profile_dataframe
from analysis.stats import descriptive, time_trend
from components.state import get_context
from components.ui import download_buttons, show_dataframe, warning_box
from models.schemas import SourceRecipe
from security.errors import AppError, to_user_message
from security.sql_validator import quote_ident
from tools.context import CSVSource

ctx = get_context()
page_header("CSV analysis", "Upload a file to check its types and quality, explore trends, or compare it with a database table.")

uploads = st.file_uploader("Upload CSV files", type=["csv", "txt"], accept_multiple_files=True)
for up in uploads or []:
    signature = (up.name, up.size)
    if st.session_state.get(f"loaded_{up.name}") == signature:
        continue
    try:
        loaded = load_csv(up.name, up.getvalue(), ctx.settings.max_upload_mb)
    except AppError as exc:
        st.error(f"{up.name}: {exc.user_message}")
        continue
    ctx.csv_sources[up.name] = CSVSource(up.name, loaded.raw, loaded.data, loaded.inferred_types, loaded.warnings)
    if f"csv:{up.name}" not in ctx.selected_sources:
        ctx.selected_sources.append(f"csv:{up.name}")
    st.session_state[f"loaded_{up.name}"] = signature

if not ctx.csv_sources:
    st.info("Upload a CSV to begin. Try `sample_data/branch_sales_upload.csv` from the project folder.")
    st.stop()

name = st.selectbox("File", list(ctx.csv_sources))
src = ctx.csv_sources[name]
df = src.data
st.caption(f"{len(df):,} rows × {len(df.columns)} columns · uploaded {src.uploaded_at:%Y-%m-%d %H:%M} UTC")
warning_box(src.warnings, "Loading warnings")

tab_preview, tab_types, tab_quality, tab_stats, tab_compare = st.tabs(
    ["Preview", "Column types", "Data quality", "Statistics & trends", "Compare / join with database"]
)

with tab_preview:
    show_dataframe(df.head(500))
    download_buttons(df, name.rsplit(".", 1)[0] + "_typed", key=f"dl_{name}")

with tab_types:
    st.markdown("Check the detected types. Changing a type re-reads the original values; nothing is deleted.")
    types_df = pd.DataFrame({"column": list(src.types), "detected type": list(src.types.values())})
    edited = st.data_editor(
        types_df, hide_index=True, disabled=["column"], key=f"types_{name}",
        column_config={"detected type": st.column_config.SelectboxColumn(options=list(TYPE_CHOICES), required=True)},
    )
    if st.button("Apply types", type="primary"):
        new_types = dict(zip(edited["column"], edited["detected type"]))
        converted, reports = apply_types(src.raw, new_types)
        src.data, src.types = converted, new_types
        failures = [r for r in reports if r.failed]
        src.warnings = [f"{r.failed} value(s) in '{r.column}' could not be read as {r.target} "
                        f"(e.g. {', '.join(r.examples[:3])})." for r in failures]
        st.success("Types applied.")
        st.rerun()

with tab_quality:
    keys = st.multiselect("Columns that should be unique keys (optional)", list(df.columns))
    invalid_dates = {
        col: int((src.raw[col].notna() & (src.raw[col].str.len() > 0) & src.data[col].isna()).sum())
        for col, t in src.types.items() if t == "datetime"
    }
    profile = profile_dataframe(df, keys, invalid_dates)
    c = st.columns(4)
    c[0].metric("Rows", f"{profile.rows:,}")
    c[1].metric("Duplicate rows", f"{profile.duplicate_rows:,}")
    c[2].metric("Missing cells", f"{sum(p.missing for p in profile.column_profiles):,}")
    c[3].metric("Columns with issues", sum(1 for p in profile.column_profiles if p.issues))
    warning_box(profile.warnings, "Findings")
    show_dataframe(profile.summary_table())
    if profile.duplicate_rows:
        with st.expander("Duplicate rows (examples)"):
            show_dataframe(duplicate_examples(df))
    st.caption("Proposed cleaning is never applied automatically. Download, fix at source, or ask the agent to "
               "explain options.")

with tab_stats:
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    date_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if numeric_cols:
        st.markdown("**Descriptive statistics**")
        show_dataframe(descriptive(df, numeric_cols).table.round(3))
    else:
        st.info("No numeric columns detected. Check the column types tab.")
    if date_cols and numeric_cols:
        st.markdown("**Trend**")
        a, b, c3, d = st.columns(4)
        dcol = a.selectbox("Date", date_cols)
        vcol = b.selectbox("Measure", numeric_cols)
        freq = c3.selectbox("Period", ["MS", "W", "D", "QS"], format_func={"MS": "Month", "W": "Week", "D": "Day", "QS": "Quarter"}.get)
        group = d.selectbox("Split by", ["(none)"] + [c for c in df.columns if c not in numeric_cols + date_cols])
        params = {"date_col": dcol, "value": vcol, "freq": freq, "group": None if group == "(none)" else group}
        res = time_trend(df, dcol, vcol, freq, "sum", 3, params["group"])
        warning_box(res.warnings, "Trend warnings")
        show_dataframe(res.table.round({c: 2 for c in res.table.select_dtypes('number').columns}))
        if st.button("Send this trend to the Visualisation Workspace"):
            recipe = SourceRecipe(kind="csv", csv_name=name, transforms=[{"method": "time_trend", "params": params}])
            record = ctx.add_result(res.table, f"{vcol} trend ({name})", recipe, f"CSV {name}", warnings=res.warnings)
            st.session_state.viz_source = record.id
            st.success("Open the Visualisation Workspace to chart it.")

with tab_compare:
    if not ctx.database_available:
        st.info("Connect the database to compare or join with a table.")
    else:
        metadata, executor = ctx.metadata, ctx.executor
        tables = [t.qualified for t in metadata.list_tables()]
        target = st.selectbox("Database table", tables)
        limit = st.number_input("Rows to read from the table", 100, ctx.settings.max_rows, min(5000, ctx.settings.max_rows), step=500)
        if st.button("Load table for comparison"):
            schema, table = target.split(".", 1)
            sql = f"SELECT * FROM {quote_ident(schema)}.{quote_ident(table)}"
            try:
                result = executor.run(sql, max_rows=int(limit), source="csv_compare")
                record = ctx.add_result(result.data, f"{target} (for comparison)", SourceRecipe(kind="sql", sql=sql),
                                        f"{ctx.settings.pg_database}.{target}", sql=result.sql, warnings=result.warnings)
                st.session_state.compare_result = record.id
                warning_box(result.warnings)
            except AppError as exc:
                st.error(exc.user_message)
        rid = st.session_state.get("compare_result")
        if rid in ctx.results:
            db_df = ctx.results[rid].data
            st.markdown("**Suggested key pairs** (you must choose; nothing is joined automatically)")
            candidates = suggest_join_keys(df, db_df)
            if candidates:
                show_dataframe(pd.DataFrame([c.__dict__ for c in candidates]))
            k1, k2, k3 = st.columns(3)
            csv_key = k1.selectbox("CSV key", list(df.columns),
                                   index=list(df.columns).index(candidates[0].left_key) if candidates else 0)
            db_key = k2.selectbox("Table key", list(db_df.columns),
                                  index=list(db_df.columns).index(candidates[0].right_key) if candidates else 0)
            how = k3.selectbox("Join type", ["left", "inner", "outer", "right"])
            try:
                report = join_report(df, db_df, csv_key, db_key, how, "CSV", "table")
            except Exception as exc:
                st.error(to_user_message(exc, "join check"))
                st.stop()
            m = st.columns(5)
            m[0].metric("CSV rows", f"{report.left_rows:,}")
            m[1].metric("Table rows", f"{report.right_rows:,}")
            m[2].metric("Matched CSV rows", f"{report.matched_left_rows:,}")
            m[3].metric("Unmatched CSV rows", f"{report.unmatched_left_rows:,}")
            m[4].metric("Result rows", f"{report.result_rows:,}", delta=f"×{report.multiplication_factor}", delta_color="off")
            st.caption(f"Relationship: {report.relationship} · duplicated keys: CSV {report.left_duplicate_keys}, "
                       f"table {report.right_duplicate_keys}")
            warning_box(report.warnings, "Join risks")
            approve = st.checkbox("I have reviewed the join report and approve this key pair")
            c1, c2 = st.columns(2)
            if c1.button("Compare values", disabled=not approve):
                comparison = compare_frames(df, db_df, csv_key, db_key, "csv", "table")
                st.write({k: v for k, v in comparison.items() if k not in ("difference_examples", "join")})
                if len(comparison["difference_examples"]):
                    show_dataframe(comparison["difference_examples"])
            if c2.button("Create joined result", disabled=not approve, type="primary"):
                merged, rep = join_frames(df, db_df, csv_key, db_key, how, "csv", "table")
                ctx.approved_joins.add((f"csv:{name}", rid, csv_key, db_key))
                record = ctx.add_result(merged, f"{name} ⋈ {target}", SourceRecipe(kind="csv", csv_name=name),
                                        f"CSV {name} + {target}", warnings=rep.warnings, refreshable=False)
                st.session_state.viz_source = record.id
                st.success(f"Joined result created ({len(merged):,} rows). Use it in the Visualisation Workspace "
                           f"or ask the agent about result `{record.id}`.")
                download_buttons(merged, f"{name}_joined", key=f"join_{record.id}")
