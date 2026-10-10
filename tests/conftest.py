"""Shared fixtures.

Live-database tests run only when TEST_PG_PASSWORD is set (plus optional
TEST_PG_HOST/PORT/DATABASE/USER and TEST_PG_SUPERUSER_PASSWORD for the
superuser-refusal test). Everything else runs offline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import SecretStr  # noqa: E402

from config import Settings  # noqa: E402
from security.sql_validator import SQLValidator, TableCatalog  # noqa: E402
from storage.app_store import AppStore  # noqa: E402
from tools.context import AgentContext  # noqa: E402

# Live tests use TEST_PG_* variables, or the app's own .env when TEST_FROM_ENV=1
# (that is what scripts/verify.bat does, so no password is typed on the command line).
TEST_FROM_ENV = os.environ.get("TEST_FROM_ENV") == "1"
LIVE_DB = bool(os.environ.get("TEST_PG_PASSWORD")) or TEST_FROM_ENV
requires_db = pytest.mark.skipif(not LIVE_DB, reason="Set TEST_PG_PASSWORD (or TEST_FROM_ENV=1) for live tests")

CATALOG_PAIRS = [
    ("training", "demo_sales"), ("training", "demo_branches"), ("training", "demo_products"),
    ("gis", "wards"), ("gis", "health_facilities"), ("Salesdata", "orders"),
    ("public", "lookup"), ("data_science", "lookup"),
]


@pytest.fixture()
def validator() -> SQLValidator:
    return SQLValidator(TableCatalog.from_pairs(CATALOG_PAIRS), max_rows=1000, database_name="powell_analytics")


@pytest.fixture()
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        pg_password="",
        openai_api_key="",
        app_data_dir=tmp_path / "app_data",
        dashboards_dir=tmp_path / "dashboards",
        max_rows=1000,
    )


@pytest.fixture()
def store(tmp_settings: Settings) -> AppStore:
    tmp_settings.ensure_dirs()
    return AppStore(tmp_settings.app_data_dir / "store.sqlite3", json_dir=tmp_settings.dashboards_dir)


@pytest.fixture()
def offline_ctx(tmp_settings: Settings, store: AppStore) -> AgentContext:
    return AgentContext(settings=tmp_settings, store=store, selected_sources=["all_csv"])


@pytest.fixture()
def sample_csv_bytes() -> bytes:
    return (ROOT / "sample_data" / "branch_sales_upload.csv").read_bytes()


@pytest.fixture(scope="session")
def live_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    if not LIVE_DB:
        pytest.skip("live database not configured")
    tmp = tmp_path_factory.mktemp("live")
    if TEST_FROM_ENV and not os.environ.get("TEST_PG_PASSWORD"):
        app = Settings()  # reads the project's .env
        return app.model_copy(update={
            "openai_api_key": SecretStr(""),
            "app_data_dir": tmp / "app_data", "dashboards_dir": tmp / "dashboards",
            "max_rows": 1000, "statement_timeout_ms": 3000,
        })
    return Settings(
        _env_file=None,
        pg_host=os.environ.get("TEST_PG_HOST", "localhost"),
        pg_port=int(os.environ.get("TEST_PG_PORT", "5432")),
        pg_database=os.environ.get("TEST_PG_DATABASE", "powell_analytics"),
        pg_user=os.environ.get("TEST_PG_USER", "powell_agent_ro"),
        pg_password=os.environ["TEST_PG_PASSWORD"],
        pg_sslmode=os.environ.get("TEST_PG_SSLMODE", "prefer"),
        openai_api_key="",
        app_data_dir=tmp / "app_data",
        dashboards_dir=tmp / "dashboards",
        max_rows=1000,
        statement_timeout_ms=3000,
    )


@pytest.fixture(scope="session")
def live_services(live_settings: Settings):
    from services import build_services

    services = build_services(live_settings)
    if not services.database_ready:
        pytest.fail(f"Live database not ready: {services.db_error}")
    return services


@pytest.fixture()
def live_ctx(live_services) -> AgentContext:
    from services import new_context

    ctx = new_context(live_services)
    ctx.selected_sources = ["database", "all_csv"]
    return ctx


def make_sales_frame() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=120, freq="D")
    return pd.DataFrame({
        "sale_date": list(dates) * 2,
        "branch": ["North"] * 120 + ["South"] * 120,
        "revenue": [100 + i for i in range(120)] + [80 + i * 0.5 for i in range(120)],
        "units": list(range(120)) * 2,
    })


@pytest.fixture()
def demo_sales(live_services):
    """Skip tests that need sample_data/demo_seed_sales.sql when it has not been loaded."""
    if live_services.metadata.resolve_table("training", "demo_sales") is None:
        pytest.skip("Run sample_data/demo_seed_sales.sql to create the demo sales tables")
