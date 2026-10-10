"""Chat with Data: natural-language analysis over the selected sources."""

from __future__ import annotations

import streamlit as st

from components.branding import ASSISTANT_AVATAR, page_header, status_chips, user_avatar

from agents.orchestrator import AgentTurn
from components.state import get_agent, get_context, get_services
from components.ui import chart_card, result_panel, warning_box

ctx = get_context()
page_header("Chat with your data", "Ask in plain English. Answers come from read-only queries on powell_analytics and the files you upload, with the SQL and evidence shown underneath.", status_chips(get_services()))

# ---------------------------------------------------------------- sources
options = (["database"] if ctx.database_available else []) + [f"csv:{n}" for n in ctx.csv_sources]
labels = {"database": f"Database ({ctx.settings.pg_database})", **{f"csv:{n}": f"CSV: {n}" for n in ctx.csv_sources}}
default = [s for s in ctx.selected_sources if s in options] or options[:1]
with st.sidebar:
    st.subheader("Sources for this chat")
    selected = st.multiselect("Analyse", options, default=default, format_func=lambda s: labels.get(s, s),
                              label_visibility="collapsed")
    ctx.selected_sources = selected
    if not options:
        st.info("Connect the database or upload a CSV on the CSV Analysis page.")
    if st.button("🧹 New conversation", width="stretch"):
        st.session_state.chat_log = []
        agent = st.session_state.get("agent")
        if agent:
            agent.reset()
        st.rerun()

agent = get_agent()
if agent is None:
    st.warning(st.session_state.get("agent_error", "The chat agent is unavailable."))
    st.caption("You can still use the Database Explorer, CSV Analysis and Visualisation pages.")

log: list[dict] = st.session_state.setdefault("chat_log", [])


def render_turn(turn: AgentTurn, index: int) -> None:
    st.markdown(turn.answer)
    if turn.unverified:
        st.warning(
            "These figures in the answer were not found in any tool output and are unverified: "
            + ", ".join(turn.unverified[:10])
        )
    for chart_id in turn.chart_ids:
        chart = ctx.charts.get(chart_id)
        if chart:
            chart_card(ctx, chart, key=f"{index}_{chart_id}")
    shown = set()
    for result_id in turn.result_ids:
        record = ctx.results.get(result_id)
        if not record or result_id in shown:
            continue
        shown.add(result_id)
        with st.expander(f"📋 {record.label} ({len(record.data):,} rows)"):
            result_panel(ctx, record, key=f"{index}_{result_id}", allow_download=result_id in ctx.export_offers)
    warning_box(turn.warnings)
    rejected = [e for e in turn.events if not e.ok and e.tool in ("execute_read_only_sql", "validate_sql")]
    with st.expander(f"🔍 Analysis steps ({len(turn.events)})"):
        for e in turn.events:
            icon = "✅" if e.ok else "⛔"
            st.markdown(f"{icon} `{e.tool}` {e.summary}")
            if e.sql:
                st.code(e.sql, language="sql")
        if rejected:
            st.caption("⛔ steps were blocked by validation or the database and corrected by the agent where possible.")


def render_approvals() -> None:
    for approval in list(ctx.pending_approvals.values()):
        with st.container(border=True):
            st.markdown(f"**Approval needed:** {approval.description}")
            report = approval.payload.get("report", {})
            cols = st.columns(4)
            cols[0].metric("Left rows", f"{report.get('left_rows', 0):,}")
            cols[1].metric("Matched", f"{report.get('matched_left_rows', 0):,}")
            cols[2].metric("Unmatched (left)", f"{report.get('unmatched_left_rows', 0):,}")
            cols[3].metric("Result rows", f"{report.get('result_rows', 0):,}",
                           delta=f"×{report.get('multiplication_factor', 1)}", delta_color="off")
            for w in report.get("warnings", []):
                st.markdown(f"- ⚠️ {w}")
            a, b = st.columns(2)
            if a.button("✅ Approve join", key=f"approve_{approval.id}", type="primary"):
                p = approval.payload
                ctx.approved_joins.add((p["left_source"], p["right_source"], p["left_key"], p["right_key"]))
                ctx.pending_approvals.pop(approval.id)
                st.session_state.queued_prompt = (
                    f"I approve the {p['how']} join of {p['left_source']}.{p['left_key']} to "
                    f"{p['right_source']}.{p['right_key']}. Please continue."
                )
                st.rerun()
            if b.button("✖ Reject", key=f"reject_{approval.id}"):
                ctx.pending_approvals.pop(approval.id)
                st.session_state.queued_prompt = "I do not approve that join. Suggest another approach."
                st.rerun()


USER_AVATAR = user_avatar(ctx.settings)
for i, entry in enumerate(log):
    with st.chat_message(entry["role"], avatar=USER_AVATAR if entry["role"] == "user" else ASSISTANT_AVATAR):
        if entry["role"] == "user":
            st.markdown(entry["content"])
        else:
            render_turn(entry["turn"], i)

render_approvals()

if not log:
    st.markdown("##### Start with one of these")
    examples = [
        "What tables are available in the database?",
        "Check gis.Birmingham for CRS and geometry problems.",
        "Create a choropleth map of health facilities per ward.",
        "What were total sales by branch last month?",
        "Forecast daily revenue for the next 30 days.",
        "Identify unusual transactions in demo_sales.",
    ]
    cols = st.columns(2)
    for i, example in enumerate(examples):
        if cols[i % 2].button(example, key=f"ex_{i}", width="stretch", disabled=agent is None):
            st.session_state.queued_prompt = example
            st.rerun()

prompt = st.chat_input("Ask a question about your data…", disabled=agent is None or not selected)
prompt = prompt or st.session_state.pop("queued_prompt", None)
if prompt and agent is not None:
    log.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        with st.spinner("Analysing…"):
            turn = agent.ask(prompt)
        log.append({"role": "assistant", "turn": turn})
        render_turn(turn, len(log) - 1)
    if ctx.pending_approvals:
        st.rerun()
