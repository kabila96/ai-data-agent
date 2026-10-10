"""Live PostgreSQL tests: role restrictions, executor, metadata, audit.

These prove the database itself refuses writes even if the validator were
bypassed. Run with TEST_PG_PASSWORD (and friends) set; see tests/conftest.py.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from security.errors import SQLValidationError
from tests.conftest import requires_db

pytestmark = requires_db


def test_security_self_check_passes(live_services):
    report = live_services.security
    assert report is not None and report.safe, report.problems()
    assert not report.is_superuser and report.session_read_only


@pytest.mark.parametrize("sql", [
    "DROP TABLE training.demo_sales",
    "DELETE FROM training.demo_sales",
    "INSERT INTO training.demo_branches VALUES (99, 'x', 'x', 'x', '2020-01-01')",
    "UPDATE training.demo_sales SET revenue = 0",
    "CREATE TABLE training.hack (id int)",
    "CREATE TEMP TABLE hack (id int)",
    "SET SESSION AUTHORIZATION postgres",
    "SET ROLE postgres",
    "SELECT pg_read_file('postgresql.conf')",
    "TRUNCATE training.demo_sales",
    "SELECT * FROM secret_hr.salaries",
])
def test_database_role_blocks_writes_even_without_validator(live_services, sql):
    """Bypass the validator entirely and send raw SQL through the app's engine."""
    with live_services.engine.connect() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(text(sql))
        conn.rollback()


def test_cannot_disable_read_only_and_write(live_services):
    with live_services.engine.connect() as conn:
        # Even if a session flips its own default, the role has no write privilege.
        conn.execute(text("SET default_transaction_read_only = off"))
        conn.commit()
        with pytest.raises(DBAPIError):
            conn.execute(text("DELETE FROM training.demo_sales WHERE sale_id = 1"))
        conn.rollback()


def test_statement_timeout_enforced(live_services):
    with live_services.engine.connect() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 500"))
        with pytest.raises(DBAPIError, match="statement timeout"):
            conn.execute(text("SELECT pg_sleep(3)"))
        conn.rollback()


def test_executor_runs_and_caps(live_services, demo_sales):
    result = live_services.executor.run("SELECT * FROM training.demo_sales")
    assert result.row_count == 1000 and result.truncated
    assert any("row limit" in w for w in result.warnings)


def test_executor_aggregate_matches_direct_sum(live_services, demo_sales):
    q = "SELECT SUM(revenue) AS total FROM training.demo_sales WHERE currency = 'GBP'"
    via_app = live_services.executor.run(q).data["total"].iloc[0]
    with live_services.engine.connect() as conn:
        direct = float(conn.execute(text(q)).scalar())
    assert abs(via_app - direct) < 1e-6


def test_executor_rejects_and_audits(live_services, live_settings):
    with pytest.raises(SQLValidationError):
        live_services.executor.run("DELETE FROM training.demo_sales")
    audit = live_services.store.recent_audit(5)
    assert audit[0]["verdict"] == "rejected"
    assert live_settings.pg_password.get_secret_value() not in str(audit)


def test_parameterised_filters(live_services, demo_sales):
    result = live_services.executor.run_wrapped(
        "SELECT s.sale_date, b.branch_name, s.revenue FROM training.demo_sales s "
        "JOIN training.demo_branches b USING (branch_id)",
        '"branch_name" IN :names AND "sale_date" >= :start',
        {"names": ["Camden", "Digbeth"], "start": date(2026, 9, 1)},
    )
    assert set(result.data["branch_name"]) <= {"Camden", "Digbeth"}
    assert result.data["sale_date"].min() >= date(2026, 9, 1)


def test_injection_through_parameter_is_inert(live_services, demo_sales):
    result = live_services.executor.run(
        "SELECT count(*) AS n FROM training.demo_branches WHERE branch_name = :name",
        params={"name": "x'; DROP TABLE training.demo_sales; --"},
    )
    assert int(result.data["n"].iloc[0]) == 0
    assert live_services.executor.run("SELECT count(*) AS n FROM training.demo_sales").data["n"].iloc[0] > 0


def test_metadata_only_approved_schemas(live_services, demo_sales):
    md = live_services.metadata
    assert "secret_hr" not in md.list_schemas()
    names = {t.qualified for t in md.list_tables()}
    assert "training.demo_sales" in names
    assert not any(n.startswith("secret_hr.") for n in names)
    info = md.describe_table("training", "demo_sales")
    assert info.primary_key == ["sale_id"]
    assert {fk.references_table for fk in info.foreign_keys} == {"training.demo_branches", "training.demo_products"}
    assert md.search("revenue")


def test_blocked_tables_setting(live_settings):
    from services import build_services

    settings = live_settings.model_copy(update={"blocked_tables_raw": "training.demo_stock"})
    services = build_services(settings)
    assert "training.demo_stock" not in {t.qualified for t in services.metadata.list_tables()}
    with pytest.raises(SQLValidationError):
        services.executor.run("SELECT * FROM training.demo_stock")


@pytest.mark.skipif(not os.environ.get("TEST_PG_SUPERUSER_PASSWORD"), reason="superuser password not provided")
def test_superuser_connection_refused(live_settings):
    from services import build_services

    settings = live_settings.model_copy(update={
        "pg_user": os.environ.get("TEST_PG_SUPERUSER", "postgres"),
        "pg_password": SecretStr(os.environ["TEST_PG_SUPERUSER_PASSWORD"]),
    })
    services = build_services(settings)
    assert not services.database_ready
    assert "SUPERUSER" in services.db_error


def test_wrong_password_message_is_safe(live_settings):
    from services import build_services

    settings = live_settings.model_copy(update={"pg_password": SecretStr("definitely-wrong-pw")})
    services = build_services(settings)
    assert not services.database_ready
    assert "definitely-wrong-pw" not in (services.db_error or "")
    assert "login failed" in services.db_error.lower() or "failed" in services.db_error.lower()


def test_table_name_accepted_as_analysis_source(live_ctx, demo_sales):
    from tools import registry

    out = registry.dispatch(live_ctx, "profile_data_quality", {"source": "training.demo_branches"})
    assert out.ok, out.payload
    assert out.payload["rows"] == 5
