"""Application wiring that is independent of the UI framework.

`build_services()` creates the shared, process-wide objects (settings, store,
database engine). `new_context()` creates per-user session state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine

from config import Settings, get_settings
from database.engine import SecurityReport, build_engine, security_self_check
from database.executor import QueryExecutor
from database.metadata import MetadataService
from security.errors import to_user_message
from security.redaction import configure_logging
from storage.app_store import AppStore
from tools.context import AgentContext

logger = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    store: AppStore
    engine: Engine | None = None
    metadata: MetadataService | None = None
    executor: QueryExecutor | None = None
    security: SecurityReport | None = None
    db_error: str | None = None
    notices: list[str] = field(default_factory=list)

    @property
    def database_ready(self) -> bool:
        return self.executor is not None


def build_services(settings: Settings | None = None, connect: bool = True) -> Services:
    settings = settings or get_settings()
    configure_logging(settings.app_data_dir / "logs", settings.log_level, settings.secret_values())
    store = AppStore(settings.app_data_dir / "app_store.sqlite3", json_dir=settings.dashboards_dir)
    services = Services(settings=settings, store=store)
    if connect:
        connect_database(services)
    return services


def connect_database(services: Services) -> None:
    """(Re)connect to PostgreSQL and verify the role is genuinely restricted."""
    settings = services.settings
    services.engine = services.metadata = services.executor = None
    services.db_error, services.security, services.notices = None, None, []
    if not settings.database_configured:
        services.db_error = "Database password is not configured. Copy .env.example to .env and set PG_PASSWORD."
        return
    try:
        engine = build_engine(settings)
        report = security_self_check(engine)
    except Exception as exc:
        services.db_error = to_user_message(exc, "database connection")
        return
    services.security = report
    if not report.safe:
        engine.dispose()
        services.db_error = (
            "Connection refused by the application's security check: "
            + " ".join(report.problems())
            + " Run setup_readonly_user.sql and connect as the read-only role."
        )
        return
    services.engine = engine
    services.metadata = MetadataService(engine, settings)
    services.executor = QueryExecutor(engine, services.metadata, settings, store=services.store)
    missing = services.metadata.missing_schemas()
    if missing:
        services.notices.append(
            "Approved schemas not found or not readable: " + ", ".join(missing)
            + ". Check APPROVED_SCHEMAS and the grants in setup_readonly_user.sql."
        )


def new_context(services: Services) -> AgentContext:
    return AgentContext(
        settings=services.settings, store=services.store,
        metadata=services.metadata, executor=services.executor,
    )
