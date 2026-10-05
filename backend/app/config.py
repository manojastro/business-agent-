"""Validated runtime configuration.

All settings come from the environment (or a .env file one or two directories up when
running locally). Real-model mode fails fast when its configuration is incomplete.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when configuration is invalid or incomplete."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_base_url: str = "http://localhost:8080"
    session_secret: str = Field(min_length=16)
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    trusted_proxies: str = "127.0.0.1"
    rate_limit_per_minute: int = 120
    max_request_bytes: int = 1_048_576
    demo_as_of_date: date = date(2026, 9, 1)
    session_ttl_hours: int = 12

    app_database_url: str
    analytics_database_url: str
    analytics_owner_database_url: str | None = None

    model_mode: Literal["fixture", "real"] = "fixture"
    model_provider: Literal["azure_openai", "openai_compatible"] = "azure_openai"
    model_name: str = ""
    model_endpoint: str = ""
    model_api_key: str = ""
    model_api_version: str = "2024-10-21"
    model_timeout_seconds: float = 60
    model_max_retries: int = 2
    model_input_cost_per_1k: Decimal = Decimal("0")
    model_output_cost_per_1k: Decimal = Decimal("0")

    max_run_cost: Decimal = Decimal("0.50")
    max_source_queries: int = Field(default=12, ge=1, le=12)
    max_hypotheses: int = Field(default=8, ge=1, le=8)
    max_plan_repairs: int = Field(default=2, ge=0, le=2)
    source_statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    source_max_rows: int = Field(default=500, ge=1, le=5000)
    source_max_result_bytes: int = Field(default=262_144, ge=1024)

    worker_concurrency: int = Field(default=2, ge=1, le=8)
    worker_lease_seconds: int = Field(default=30, ge=5)
    worker_heartbeat_seconds: int = Field(default=10, ge=1)

    demo_user_password: str = ""
    otel_exporter_otlp_endpoint: str = ""

    @field_validator("session_secret")
    @classmethod
    def _secret_not_placeholder(cls, v: str) -> str:
        if v.startswith("change-me"):
            raise ValueError("SESSION_SECRET still has the placeholder value")
        return v

    @model_validator(mode="after")
    def _check_real_mode(self) -> Settings:
        if self.model_mode == "real":
            missing = [
                name
                for name, value in (
                    ("MODEL_NAME", self.model_name),
                    ("MODEL_ENDPOINT", self.model_endpoint),
                    ("MODEL_API_KEY", self.model_api_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "MODEL_MODE=real requires " + ", ".join(missing)
                    + ". Set them or use MODEL_MODE=fixture."
                )
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be smaller than WORKER_LEASE_SECONDS")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError -> readable startup failure
        raise ConfigError(f"Invalid configuration: {exc}") from exc
