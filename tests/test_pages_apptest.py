"""Headless UI smoke tests with Streamlit's AppTest (runs where Streamlit is installed).

Each page must render without an uncaught exception using the settings in .env.
If the database is not configured the pages must show a message, not crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
ROOT = Path(__file__).resolve().parents[1]
PAGES = ["chat.py", "database_explorer.py", "csv_analysis.py", "visualisation_workspace.py",
         "dashboard_builder.py", "analysis_history.py"]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exception(page, monkeypatch):
    monkeypatch.chdir(ROOT)
    at = st_testing.AppTest.from_file(str(ROOT / "pages" / page), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def test_entry_point_renders(monkeypatch):
    monkeypatch.chdir(ROOT)
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
