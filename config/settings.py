"""Application settings using Pydantic Settings."""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: str = Field(default="development")
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_broker_url: str = Field(default="redis://localhost:6379/0")
    celery_result_backend: str = Field(default="redis://localhost:6379/0")

    # PostgreSQL
    database_url: str = Field(
        default="postgresql://gemstone:gemstone@localhost:5432/gemstone"
    )

    # Storage
    storage_backend: str = Field(default="local")  # 'local' or 's3'
    s3_endpoint_url: Optional[str] = Field(default=None)
    s3_access_key: Optional[str] = Field(default=None)
    s3_secret_key: Optional[str] = Field(default=None)
    s3_bucket: str = Field(default="gemstone-data")
    s3_region: str = Field(default="us-east-1")

    # Local storage paths
    local_storage_root: str = Field(default="./data")
    raw_html_path: str = Field(default="./data/raw/html")
    images_path: str = Field(default="./data/images")
    validated_path: str = Field(default="./data/validated")

    # Crawler settings
    default_user_agent: str = Field(
        default="GemstoneBot/1.0 (+https://example.com/bot; research purposes)"
    )
    respect_robots_txt: bool = Field(default=True)
    default_crawl_delay: float = Field(default=2.0)
    max_concurrent_per_domain: int = Field(default=2)
    max_requests_per_second: float = Field(default=1.0)
    request_timeout: int = Field(default=30)
    max_retries: int = Field(default=3)

    # Compliance
    require_robots_allow: bool = Field(default=True)
    require_tos_allow: bool = Field(default=False)

    # Validation thresholds
    min_image_count: int = Field(default=2)
    min_image_resolution: int = Field(default=600)
    min_carat: float = Field(default=0.05)
    max_carat: float = Field(default=50.0)
    phash_distance_threshold: int = Field(default=8)

    # Optional LLM
    openai_api_key: Optional[str] = Field(default=None)
    llm_model: str = Field(default="gpt-3.5-turbo")
    use_llm_normalization: bool = Field(default=False)


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
