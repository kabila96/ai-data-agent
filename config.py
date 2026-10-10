"""Application settings loaded from environment variables / `.env`.

Secrets are held as `SecretStr` so they never appear in reprs, logs or the UI.
Nothing in this module should ever be sent to the language model.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    """Typed configuration. See `.env.example` for documentation of each value."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- PostgreSQL / PostGIS (read-only role) ---
    pg_host: str = "localhost"
    pg_port: int = Field(default=5432, ge=1, le=65535)
    pg_database: str = "powell_analytics"
    pg_user: str = "powell_agent_ro"
    pg_password: SecretStr = SecretStr("")
    pg_sslmode: str = "prefer"
    pg_connect_timeout_s: int = Field(default=10, ge=1, le=120)

    # Stored as comma-separated strings so they are easy to edit in `.env`.
    approved_schemas_raw: str = Field(
        default="salesdata,data_science,gis,public,training", alias="APPROVED_SCHEMAS"
    )
    blocked_tables_raw: str = Field(default="", alias="BLOCKED_TABLES")

    max_rows: int = Field(default=5000, ge=1, le=200_000)
    statement_timeout_ms: int = Field(default=15_000, ge=500, le=600_000)

    # --- OpenAI ---
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5.6-luna"
    openai_base_url: str | None = None
    # "responses" (default; needed for tools + reasoning on newer models) or "chat" (Chat Completions)
    openai_api_style: str = "responses"
    openai_reasoning_effort: str | None = None  # blank = model default; or low / medium / high
    llm_max_sample_rows: int = Field(default=20, ge=0, le=200)
    agent_max_steps: int = Field(default=12, ge=1, le=40)

    # --- GIS ---
    default_crs_epsg: int = 4326
    british_crs_epsg: int = 27700
    metric_crs_override_epsg: int | None = None

    # --- Access control (set this whenever the app is reachable by other people) ---
    app_password: SecretStr = SecretStr("")

    # --- Branding ---
    brand_name: str = "Powell A. Ndlovu"
    brand_tagline: str = "GIS, remote sensing and data analytics"
    brand_logo_path: str | None = None  # default: assets/logo.jpg|png|webp

    # --- Application storage ---
    app_data_dir: Path = PROJECT_ROOT / "app_data"
    dashboards_dir: Path = PROJECT_ROOT / "saved_dashboards"
    max_upload_mb: int = Field(default=200, ge=1, le=2000)
    log_level: str = "INFO"

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # The project's .env wins over Windows/system environment variables, so an old
        # OPENAI_API_KEY or PG_PASSWORD set system-wide cannot silently override it.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    @field_validator("pg_password", "openai_api_key", mode="before")
    @classmethod
    def _clean_secret(cls, value: object) -> object:
        """Remove stray whitespace, surrounding quotes and invisible characters from pasted secrets."""
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            value = value.strip().strip("\ufeff\u200b").strip().strip('"').strip("'").strip()
        return value

    @field_validator("pg_sslmode")
    @classmethod
    def _check_sslmode(cls, value: str) -> str:
        allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if value not in allowed:
            raise ValueError(f"PG_SSLMODE must be one of {sorted(allowed)}")
        return value

    @field_validator("openai_api_style")
    @classmethod
    def _check_api_style(cls, value: str) -> str:
        value = (value or "responses").strip().lower()
        if value not in ("responses", "chat"):
            raise ValueError("OPENAI_API_STYLE must be 'responses' or 'chat'")
        return value

    @field_validator("openai_reasoning_effort", "openai_base_url", "brand_logo_path", mode="before")
    @classmethod
    def _blank_str_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("metric_crs_override_epsg", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @property
    def approved_schemas(self) -> list[str]:
        return _split_csv(self.approved_schemas_raw)

    @property
    def blocked_tables(self) -> set[str]:
        """Fully qualified `schema.table` names (lower-case) that are never exposed."""
        return {t.lower() for t in _split_csv(self.blocked_tables_raw)}

    @property
    def database_configured(self) -> bool:
        return bool(self.pg_password.get_secret_value())

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key.get_secret_value())

    def secret_values(self) -> list[str]:
        """Raw secret strings, used only by the log-redaction filter."""
        return [
            s
            for s in (self.pg_password.get_secret_value(), self.openai_api_key.get_secret_value(),
                      self.app_password.get_secret_value())
            if s
        ]

    def ensure_dirs(self) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.dashboards_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
