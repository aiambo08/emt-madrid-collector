from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL


def _parse_csv(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, int | float):
        value = str(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    raise TypeError(f"cannot parse list from {value!r}")


class ConfigError(ValueError):
    """A command-specific configuration requirement is not met."""


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    emt_base_url: str = "https://openapi.emtmadrid.es"
    emt_email: str | None = None
    emt_password: str | None = None
    emt_client_id: str | None = None
    emt_pass_key: str | None = None

    emt_lines: Annotated[list[str], NoDecode] = Field(default_factory=list)
    emt_stops: Annotated[list[str], NoDecode] = Field(default_factory=list)
    emt_stops_refresh_hours: int = 24

    collect_interval_seconds: int = Field(60, gt=0)
    emt_max_requests_per_minute: int = 100
    emt_daily_request_budget: int = 150_000
    emt_request_timeout_seconds: float = 15.0
    emt_max_retries: int = 4

    # Either a full SQLAlchemy URL, or the POSTGRES_* parts (the URL is then built with
    # proper escaping, so the password may contain any character).
    database_url: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "emt"
    postgres_password: str | None = None
    postgres_db: str = "emt"
    db_use_timescale: bool = True

    log_level: str = "INFO"
    log_format: str = "json"

    @field_validator("emt_lines", "emt_stops", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> list[str]:
        return _parse_csv(value)

    @field_validator("log_format")
    @classmethod
    def _check_log_format(cls, value: str) -> str:
        value = value.lower()
        if value not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be 'json' or 'console'")
        return value

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if not self.postgres_password:
            raise ConfigError("Set DATABASE_URL, or POSTGRES_PASSWORD (+ POSTGRES_HOST/USER/DB)")
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @property
    def has_credentials(self) -> bool:
        has_user = bool(self.emt_email and self.emt_password)
        has_app = bool(self.emt_client_id and self.emt_pass_key)
        return has_user or has_app

    @property
    def has_targets(self) -> bool:
        return bool(self.emt_lines or self.emt_stops)

    def require_credentials(self) -> None:
        """Needed by every command that talks to the EMT API."""
        if not self.has_credentials:
            raise ConfigError(
                "Set EMT_EMAIL + EMT_PASSWORD or EMT_CLIENT_ID + EMT_PASS_KEY (see .env.example)"
            )

    def require_targets(self) -> None:
        """Needed by collection commands (`run`, `once`, `check`)."""
        if not self.has_targets:
            raise ConfigError("Set EMT_LINES and/or EMT_STOPS; polling every stop is not viable")

    def require_interval(self) -> None:
        """Needed by the scheduler (`run`)."""
        if self.collect_interval_seconds < 10:
            raise ConfigError("COLLECT_INTERVAL_SECONDS must be >= 10")

    @property
    def cycles_per_day(self) -> float:
        return 86_400 / self.collect_interval_seconds
