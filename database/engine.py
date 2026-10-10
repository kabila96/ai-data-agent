"""SQLAlchemy engine for the read-only analysis role.

The connection URL is assembled with `URL.create` so the password is never
formatted into a string that could reach logs. Every session is forced
read-only at connection time, in addition to the role-level default set by
`setup_readonly_user.sql`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from config import Settings
from security.errors import DataSourceError

logger = logging.getLogger(__name__)


def build_engine(settings: Settings) -> Engine:
    if not settings.database_configured:
        raise DataSourceError("Database password is not configured. Set PG_PASSWORD in .env.")
    url = URL.create(
        drivername="postgresql+psycopg",
        username=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
    )
    options = (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={int(settings.statement_timeout_ms)} "
        "-c idle_in_transaction_session_timeout=60000"
    )
    return create_engine(
        url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        hide_parameters=True,  # never echo bound values in exceptions
        connect_args={
            "options": options,
            "application_name": "ai_data_agent",
            "connect_timeout": settings.pg_connect_timeout_s,
            "sslmode": settings.pg_sslmode,
        },
    )


@dataclass
class SecurityReport:
    """Result of checking that the connected role really is restricted."""

    role: str = ""
    server_version: str = ""
    is_superuser: bool = False
    session_read_only: bool = False
    can_create_in_db: bool = False
    writable_tables: list[str] = field(default_factory=list)
    creatable_schemas: list[str] = field(default_factory=list)
    dangerous_memberships: list[str] = field(default_factory=list)
    postgis_version: str | None = None

    @property
    def safe(self) -> bool:
        return (
            not self.is_superuser
            and self.session_read_only
            and not self.can_create_in_db
            and not self.writable_tables
            and not self.creatable_schemas
            and not self.dangerous_memberships
        )

    def problems(self) -> list[str]:
        issues: list[str] = []
        if self.is_superuser:
            issues.append(f"Role '{self.role}' is a SUPERUSER. Use the dedicated read-only role.")
        if not self.session_read_only:
            issues.append("Session is not read-only.")
        if self.can_create_in_db:
            issues.append("Role can CREATE schemas in the database.")
        if self.creatable_schemas:
            issues.append("Role can CREATE objects in: " + ", ".join(self.creatable_schemas))
        if self.writable_tables:
            sample = ", ".join(self.writable_tables[:5])
            issues.append(f"Role has write privileges on {len(self.writable_tables)} table(s), e.g. {sample}")
        if self.dangerous_memberships:
            issues.append("Role is a member of: " + ", ".join(self.dangerous_memberships))
        return issues


_SECURITY_SQL = {
    "basic": """
        SELECT current_user AS role,
               current_setting('server_version') AS server_version,
               r.rolsuper AS is_superuser,
               current_setting('transaction_read_only') = 'on' AS session_read_only,
               has_database_privilege(current_database(), 'CREATE') AS can_create_in_db
        FROM pg_roles r WHERE r.rolname = current_user
    """,
    "writable": """
        SELECT table_schema || '.' || table_name AS t
        FROM information_schema.table_privileges
        WHERE grantee IN (SELECT rolname FROM pg_roles WHERE pg_has_role(current_user, oid, 'MEMBER'))
          AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')
          AND table_schema NOT IN ('pg_catalog','information_schema')
        GROUP BY 1 ORDER BY 1 LIMIT 50
    """,
    "creatable": """
        SELECT nspname FROM pg_namespace
        WHERE has_schema_privilege(oid, 'CREATE')
          AND nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'
        ORDER BY 1
    """,
    "members": """
        SELECT b.rolname FROM pg_roles b
        WHERE pg_has_role(current_user, b.oid, 'MEMBER')
          AND b.rolname IN ('pg_write_all_data','pg_read_server_files','pg_write_server_files',
                            'pg_execute_server_program','pg_signal_backend','pg_database_owner')
    """,
    "postgis": "SELECT extversion FROM pg_extension WHERE extname = 'postgis'",
}


def security_self_check(engine: Engine) -> SecurityReport:
    """Inspect the connected role's privileges using fixed catalog queries."""
    report = SecurityReport()
    with engine.connect() as conn:
        row = conn.execute(text(_SECURITY_SQL["basic"])).mappings().one()
        report.role = row["role"]
        report.server_version = row["server_version"]
        report.is_superuser = bool(row["is_superuser"])
        report.session_read_only = bool(row["session_read_only"])
        report.can_create_in_db = bool(row["can_create_in_db"])
        report.writable_tables = [r[0] for r in conn.execute(text(_SECURITY_SQL["writable"]))]
        report.creatable_schemas = [r[0] for r in conn.execute(text(_SECURITY_SQL["creatable"]))]
        report.dangerous_memberships = [r[0] for r in conn.execute(text(_SECURITY_SQL["members"]))]
        report.postgis_version = conn.execute(text(_SECURITY_SQL["postgis"])).scalar()
        conn.rollback()
    if not report.safe:
        logger.warning("Database security self-check failed: %s", "; ".join(report.problems()))
    return report
