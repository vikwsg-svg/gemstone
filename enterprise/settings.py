"""Configuration for the enterprise API (environment variables prefixed ``ENTERPRISE_``)."""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnterpriseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENTERPRISE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Storage. Defaults to a local SQLite file for development; production uses Postgres.
    database_url: str = "sqlite:///./data/enterprise.db"

    # Pricing engine used for *live* tenants. Sandbox tenants always use the
    # synthetic sandbox engine.
    #   none    -> live valuations return 503 engine_unavailable (safe default)
    #   remote  -> HTTP adapter to the V2 comps engine (see enterprise/engine/remote.py)
    #   sandbox -> synthetic data for local development; responses are flagged synthetic
    engine_backend: Literal["none", "remote", "sandbox"] = "none"
    engine_url: Optional[str] = None
    engine_api_key: Optional[str] = None
    engine_timeout_seconds: float = 5.0

    # Identify engine (the scan's first step: photos -> what the item is) for live
    # tenants. Sandbox tenants always use the simulated sandbox identifier.
    identify_backend: Literal["none", "remote", "sandbox"] = "none"
    identify_url: Optional[str] = None
    identify_api_key: Optional[str] = None
    identify_timeout_seconds: float = 20.0
    # How long a request waits for a pending identification before answering 202.
    identify_wait_seconds: float = 8.0
    identify_poll_interval_seconds: float = 1.0

    # Overrides the registry's "current" model version when set.
    default_model_version: Optional[str] = None

    idempotency_ttl_hours: int = Field(default=24, ge=1)

    batch_max_rows: int = Field(default=5000, ge=1)
    batch_max_bytes: int = Field(default=5_000_000, ge=1)

    # Used as the server URL in the exported OpenAPI document.
    public_base_url: str = "https://api.gemstoneaiapp.com/enterprise/v1"


@lru_cache
def get_settings() -> EnterpriseSettings:
    return EnterpriseSettings()
