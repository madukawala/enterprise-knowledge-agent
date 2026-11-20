"""Application settings.

Settings come from environment variables, with the `.env` file as a fallback.
Every variable is listed with an example value in `.env.example`.

Use `get_settings()` instead of creating `Settings()` directly, so the
environment is read once per process.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    """One JSON object per line. Used in Docker and CI."""

    CONSOLE = "console"
    """Coloured, human-friendly lines for local development."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "enterprise-knowledge-agent"
    app_env: Environment = Environment.LOCAL

    log_level: LogLevel = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # Docker sets API_HOST=0.0.0.0 inside the container. Outside Docker the
    # API only listens on the local machine.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Connection strings for the backing services. Docker Compose sets both.
    # When one is empty, `/readyz` skips that check, so the API can still be
    # run on its own with `make run-api`. Stored as SecretStr because the
    # URLs contain passwords; they never appear in logs or error messages.
    postgres_url: SecretStr | None = None
    redis_url: SecretStr | None = None

    # How long each readiness check may take before it counts as failed.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # Request metrics for Prometheus on /metrics.
    metrics_enabled: bool = True

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once and then cached."""
    return Settings()
