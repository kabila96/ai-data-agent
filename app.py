"""Entry point: `streamlit run app.py`."""

from __future__ import annotations

import streamlit as st

from config import get_settings
from components.branding import page_icon

_settings = get_settings()
st.set_page_config(page_title=f"{_settings.brand_name} | Data agent", page_icon=page_icon(_settings),
                   layout="wide", initial_sidebar_state="expanded")

from components.branding import apply_theme, require_password, sidebar_profile  # noqa: E402
from components.state import get_context, get_services  # noqa: E402

apply_theme()
require_password(_settings)
services = get_services()
get_context()

pages = {
    "Analyse": [
        st.Page("pages/chat.py", title="Chat with data", icon=":material/forum:", default=True),
        st.Page("pages/csv_analysis.py", title="CSV analysis", icon=":material/table_view:"),
        st.Page("pages/visualisation_workspace.py", title="Visualisation workspace", icon=":material/insert_chart:"),
    ],
    "Manage": [
        st.Page("pages/database_explorer.py", title="Database explorer", icon=":material/database:"),
        st.Page("pages/dashboard_builder.py", title="Dashboards", icon=":material/dashboard:"),
        st.Page("pages/analysis_history.py", title="History", icon=":material/history:"),
    ],
}
# The built-in menu is hidden so the profile card can sit above the page links.
navigation = st.navigation(pages, position="hidden")

status: list[tuple[str, str]] = []
if services.database_ready:
    status.append(("ok", f"Connected to {services.settings.pg_database}"))
    status.append(("ok", f"Read-only login: {services.security.role}" if services.security else "Read-only login"))
    if services.security and services.security.postgis_version:
        status.append(("ok", f"PostGIS {services.security.postgis_version}"))
else:
    status.append(("off", "Database not connected"))
status.append(("ok", f"Model: {services.settings.openai_model}") if services.settings.openai_configured
              else ("warn", "OpenAI key not set; chat is off"))
sidebar_profile(services.settings, status)

with st.sidebar:
    for section, items in pages.items():
        st.caption(section)
        for page in items:
            st.page_link(page)
    st.divider()
for notice in services.notices:
    st.sidebar.warning(notice)
if services.db_error:
    st.sidebar.error(services.db_error)

navigation.run()
