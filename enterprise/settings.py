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

    # The GemstoneAI scan used for *live* tenants. Sandbox tenants always get the
    # example results of the sandbox engine.
    #   none    -> live scans return 503 scan_unavailable (safe default)
    #   remote  -> HTTP adapter to the backend's scan (see enterprise/engine/remote.py)
    #   sandbox -> example results for local development; responses are flagged synthetic
    scan_backend: Literal["none", "remote", "sandbox"] = "none"
    scan_url: Optional[str] = None
    scan_api_key: Optional[str] = None
    scan_timeout_seconds: float = 20.0
    # How long POST /scans waits for a running scan before answering "processing".
    scan_wait_seconds: float = 8.0
    scan_poll_interval_seconds: float = 1.0

    idempotency_ttl_hours: int = Field(default=24, ge=1)

    batch_max_rows: int = Field(default=5000, ge=1)
    batch_max_bytes: int = Field(default=5_000_000, ge=1)

    # Used as the server URL in the exported OpenAPI document.
    public_base_url: str = "https://api.gemstoneaiapp.com/enterprise/v1"


@lru_cache
def get_settings() -> EnterpriseSettings:
    return EnterpriseSettings()
