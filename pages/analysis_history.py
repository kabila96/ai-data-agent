"""Analysis History and query audit log (no credentials or row-level data are stored)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from components.branding import page_header

from components.state import get_context
from components.ui import show_dataframe

ctx = get_context()
page_header("Analysis history", "Every question, the SQL it ran and any warnings. No passwords or row data are stored.")

tab_history, tab_audit = st.tabs(["Questions", "SQL audit log"])

with tab_history:
    entries = ctx.store.list_history()
    if not entries:
        st.info("No analyses recorded yet.")
    else:
        status = st.multiselect("Status", ["success", "partial", "clarification", "error"],
                                default=["success", "partial", "clarification", "error"])
        query = st.text_input("Search questions")
        shown = [e for e in entries if e.status in status and (not query or query.lower() in e.question.lower())]
        st.caption(f"{len(shown)} of {len(entries)} entries")
        for e in shown[:200]:
            icon = {"success": "✅", "partial": "🟡", "clarification": "❓", "error": "⛔"}.get(e.status, "•")
            with st.expander(f"{icon} {e.created_at:%Y-%m-%d %H:%M} · {e.question[:110]}"):
                st.markdown(f"**Sources:** {', '.join(e.sources) or 'n/a'}")
                st.markdown(f"**Methods:** {', '.join(e.methods) or 'n/a'}")
                if e.charts:
                    st.markdown(f"**Charts:** {', '.join(e.charts)}")
                for sql in e.sql:
                    st.code(sql, language="sql")
                for w in e.warnings:
                    st.markdown(f"- ⚠️ {w}")
                for err in e.errors:
                    st.markdown(f"- ⛔ {err}")
        export = pd.DataFrame([e.model_dump(mode="json") for e in shown])
        st.download_button("⬇️ Export history (CSV)", export.to_csv(index=False).encode("utf-8"),
                           "analysis_history.csv", "text/csv")
        with st.popover("Clear history"):
            if st.button("Confirm: delete all history", type="primary"):
                ctx.store.clear_history()
                st.rerun()

with tab_audit:
    audit = ctx.store.recent_audit(500)
    if not audit:
        st.info("No queries recorded yet.")
    else:
        df = pd.DataFrame(audit)
        verdicts = st.multiselect("Verdict", sorted(df["verdict"].unique()), default=sorted(df["verdict"].unique()))
        show_dataframe(df[df["verdict"].isin(verdicts)][
            ["created_at", "source", "verdict", "row_count", "elapsed_ms", "detail", "sql"]
        ])
