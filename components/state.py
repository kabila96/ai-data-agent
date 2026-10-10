"""Streamlit session wiring: shared services, per-session context and agent."""

from __future__ import annotations

import streamlit as st

from agents.llm import build_llm
from agents.orchestrator import DataAgent
from security.errors import AppError
from services import Services, build_services, connect_database, new_context
from tools.context import AgentContext


@st.cache_resource(show_spinner="Connecting to services…")
def get_services() -> Services:
    return build_services()


def reconnect() -> Services:
    services = get_services()
    connect_database(services)
    ctx = st.session_state.get("ctx")
    if ctx is not None:
        ctx.metadata, ctx.executor = services.metadata, services.executor
    return services


def get_context() -> AgentContext:
    if "ctx" not in st.session_state:
        st.session_state.ctx = new_context(get_services())
    return st.session_state.ctx


def get_agent() -> DataAgent | None:
    """Return the session's agent, or None (with a reason in session state) if OpenAI is not configured."""
    if "agent" in st.session_state:
        return st.session_state.agent
    services = get_services()
    try:
        llm = build_llm(services.settings)
    except AppError as exc:
        st.session_state.agent_error = exc.user_message
        return None
    except ImportError:
        st.session_state.agent_error = "The openai package is not installed. Run: pip install -r requirements.txt"
        return None
    st.session_state.agent = DataAgent(llm, get_context())
    return st.session_state.agent
