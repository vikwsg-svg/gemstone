"""
Pydantic data models for the gemstone pipeline.

These models define the schema for all scraped items, validation results,
and compliance tracking.
"""

from datetime import datetime
from enum import IntEnum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class ValidationTier(IntEnum):
    """Validation tier levels."""

    TIER_0 = 0  # Lab report verified, highest quality
    TIER_1 = 1  # Strong structured data from trusted source
    TIER_2 = 2  # Weak labels, pretraining only
    TIER_3 = 3  # Reject


class ComplianceInfo(BaseModel):
    """Compliance tracking for a scraped item."""

    robots_allowed: bool = Field(
        description="Whether robots.txt allows crawling this URL"
    )
    tos_allowed: bool = Field(
        default=True, description="Whether ToS allows crawling (manual review)"
    )
    user_agent: str = Field(description="User agent used for the request")
    crawl_delay_respected: bool = Field(
        default=True, description="Whether crawl delay was respected"
    )
    notes: str = Field(default="", description="Additional compliance notes")


class ValidationResult(BaseModel):
    """Validation result for a gemstone item."""

    tier: ValidationTier = Field(description="Validation tier (0-3)")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence score (0.0 to 1.0)"
    )
    keep: bool = Field(description="Whether to keep this item in the dataset")
    reject_reasons: list[str] = Field(
        default_factory=list, description="Reasons for rejection or tier demotion"
    )
    sanity_checks_passed: list[str] = Field(
        default_factory=list, description="List of passed sanity checks"
    )
    sanity_checks_failed: list[str] = Field(
        default_factory=list, description="List of failed sanity checks"
    )


class ScrapedImage(BaseModel):
    """Information about a scraped image."""

    url: str = Field(description="Original image URL")
    local_path: Optional[str] = Field(
        default=None, description="Local storage path after download"
    )
    s3_key: Optional[str] = Field(default=None, description="S3 key if stored in S3")
    width: Optional[int] = Field(default=None, description="Image width in pixels")
    height: Optional[int] = Field(default=None, description="Image height in pixels")
    phash: Optional[str] = Field(default=None, description="Perceptual hash of image")
    file_size_bytes: Optional[int] = Field(default=None, description="File size")
    content_type: Optional[str] = Field(default=None, description="MIME type")
    download_status: str = Field(
        default="pending", description="Download status: pending, success, failed"
    )
    download_error: Optional[str] = Field(
        default=None, description="Error message if download failed"
    )


class DiscoveredURL(BaseModel):
    """A discovered URL pending scraping."""

    url: str = Field(description="The discovered URL")
    domain: str = Field(description="Domain of the URL")
    discovered_at_utc: datetime = Field(
        default_factory=datetime.utcnow, description="When URL was discovered"
    )
    source_type: str = Field(
        default="crawl", description="How URL was discovered: seed, crawl, sitemap"
    )
    depth: int = Field(default=0, description="Crawl depth from seed")
    priority: int = Field(default=0, description="Processing priority (higher = sooner)")
    status: str = Field(
        default="pending",
        description="Status: pending, fetching, fetched, parsing, parsed, validated, failed",
    )
    retry_count: int = Field(default=0, description="Number of retry attempts")
    last_error: Optional[str] = Field(
        default=None, description="Last error message if any"
    )


class GemstoneItem(BaseModel):
    """
    Core data model for a scraped gemstone item.

    This is the main schema that all scraped items must conform to.
    """

    # Identifiers
    item_id: str = Field(
        default_factory=lambda: str(uuid4()), description="Unique item identifier"
    )

    # Source information
    source_site: str = Field(description="Domain of the source site")
    source_url: str = Field(description="Full URL of the listing page")
    discovered_at_utc: datetime = Field(
        default_factory=datetime.utcnow, description="When URL was discovered"
    )
    scraped_at_utc: Optional[datetime] = Field(
        default=None, description="When page was scraped"
    )

    # Content
    title: str = Field(description="Listing title")
    description_text: str = Field(
        default="", description="Full description text from listing"
    )
    raw_html_path: Optional[str] = Field(
        default=None, description="Path to stored raw HTML snapshot"
    )

    # Images
    image_urls: list[str] = Field(
        default_factory=list, description="List of image URLs from listing"
    )
    downloaded_images: list[ScrapedImage] = Field(
        default_factory=list, description="Downloaded image information"
    )

    # Stone attributes
    stone_type_claimed: str = Field(
        description="Stone type as claimed on the page (e.g., 'diamond', 'ruby')"
    )
    shape: Optional[str] = Field(
        default=None, description="Shape of the stone (e.g., 'round', 'oval')"
    )
    color_family: Optional[str] = Field(
        default=None, description="Color family (e.g., 'yellow', 'blue', 'pink')"
    )
    carat: Optional[float] = Field(
        default=None, description="Carat weight if available"
    )

    # Lab certification
    lab_name: Optional[str] = Field(
        default=None, description="Certifying lab name (e.g., 'GIA', 'IGI')"
    )
    lab_report_id: Optional[str] = Field(
        default=None, description="Lab report/certificate number"
    )
    has_lab_report: bool = Field(
        default=False, description="Whether a lab report is referenced"
    )

    # Additional extracted attributes
    extracted_attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional attributes extracted from the page",
    )

    # Validation
    validation: Optional[ValidationResult] = Field(
        default=None, description="Validation result"
    )

    # Compliance
    compliance: Optional[ComplianceInfo] = Field(
        default=None, description="Compliance information"
    )

    # Deduplication
    is_duplicate: bool = Field(default=False, description="Whether this is a duplicate")
    duplicate_of: Optional[str] = Field(
        default=None, description="Item ID this is a duplicate of"
    )
    canonical_url: Optional[str] = Field(
        default=None, description="Canonicalized URL for deduplication"
    )

    @field_validator("carat")
    @classmethod
    def validate_carat(cls, v: Optional[float]) -> Optional[float]:
        """Validate carat is within plausible bounds."""
        if v is not None and (v < 0.001 or v > 1000):
            raise ValueError(f"Carat value {v} is outside plausible bounds")
        return v

    @field_validator("image_urls")
    @classmethod
    def validate_image_urls(cls, v: list[str]) -> list[str]:
        """Remove duplicate URLs while preserving order."""
        seen = set()
        unique = []
        for url in v:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    @model_validator(mode="after")
    def set_has_lab_report(self) -> "GemstoneItem":
        """Auto-set has_lab_report based on lab_report_id presence."""
        if self.lab_report_id and not self.has_lab_report:
            self.has_lab_report = True
        return self

    def get_downloaded_image_paths(self) -> list[str]:
        """Get list of successfully downloaded image paths."""
        return [
            img.local_path
            for img in self.downloaded_images
            if img.local_path and img.download_status == "success"
        ]

    def get_valid_images(self, min_resolution: int = 600) -> list[ScrapedImage]:
        """Get images that meet minimum resolution requirements."""
        return [
            img
            for img in self.downloaded_images
            if img.download_status == "success"
            and img.width
            and img.height
            and img.width >= min_resolution
            and img.height >= min_resolution
        ]


class JobStatus(BaseModel):
    """Status tracking for a crawl job."""

    job_id: str = Field(description="Unique job identifier")
    domain: str = Field(description="Domain being crawled")
    status: str = Field(
        default="pending",
        description="Job status: pending, running, paused, completed, failed",
    )
    created_at_utc: datetime = Field(default_factory=datetime.utcnow)
    started_at_utc: Optional[datetime] = Field(default=None)
    completed_at_utc: Optional[datetime] = Field(default=None)
    urls_discovered: int = Field(default=0)
    urls_fetched: int = Field(default=0)
    urls_parsed: int = Field(default=0)
    urls_validated: int = Field(default=0)
    urls_failed: int = Field(default=0)
    items_kept: int = Field(default=0)
    items_rejected: int = Field(default=0)
    error_message: Optional[str] = Field(default=None)


class DatasetStats(BaseModel):
    """Statistics for the dataset."""

    total_items: int = Field(default=0)
    items_by_tier: dict[int, int] = Field(default_factory=dict)
    items_by_stone_type: dict[str, int] = Field(default_factory=dict)
    items_by_shape: dict[str, int] = Field(default_factory=dict)
    items_by_source: dict[str, int] = Field(default_factory=dict)
    total_images: int = Field(default=0)
    avg_images_per_item: float = Field(default=0.0)
    duplicate_count: int = Field(default=0)
    duplicate_rate: float = Field(default=0.0)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    lab_report_count: int = Field(default=0)
    lab_report_rate: float = Field(default=0.0)
    generated_at_utc: datetime = Field(default_factory=datetime.utcnow)
