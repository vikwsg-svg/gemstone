"""
Schema validation for gemstone items.

Validates that items conform to the expected Pydantic schema.
"""

from typing import Any, Optional

from pydantic import ValidationError
import structlog

from scrapers.models import GemstoneItem

logger = structlog.get_logger(__name__)


class SchemaValidationError(Exception):
    """Raised when schema validation fails."""

    def __init__(self, message: str, errors: list[dict]):
        super().__init__(message)
        self.errors = errors


def validate_item_schema(data: dict[str, Any]) -> tuple[Optional[GemstoneItem], list[str]]:
    """
    Validate data against the GemstoneItem schema.

    Args:
        data: Dictionary of item data

    Returns:
        Tuple of (validated_item, list_of_errors)
        If validation succeeds, errors list is empty.
        If validation fails, item is None.
    """
    errors = []

    try:
        item = GemstoneItem(**data)
        return item, []

    except ValidationError as e:
        for error in e.errors():
            field = ".".join(str(loc) for loc in error["loc"])
            msg = error["msg"]
            errors.append(f"{field}: {msg}")

        logger.warning(
            "schema_validation_failed",
            error_count=len(errors),
            errors=errors[:5],  # Log first 5 errors
        )
        return None, errors


def validate_required_fields(item: GemstoneItem) -> list[str]:
    """
    Check that required fields are present and non-empty.

    Args:
        item: The item to validate

    Returns:
        List of missing/empty field names
    """
    missing = []

    required_fields = [
        ("source_site", item.source_site),
        ("source_url", item.source_url),
        ("title", item.title),
        ("stone_type_claimed", item.stone_type_claimed),
    ]

    for field_name, value in required_fields:
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(field_name)

    return missing


def validate_url_format(url: str) -> bool:
    """
    Validate that a string is a valid URL.

    Args:
        url: The URL string to validate

    Returns:
        True if valid URL format
    """
    if not url:
        return False

    # Basic URL validation
    return url.startswith(("http://", "https://"))


def validate_image_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    """
    Validate image URLs.

    Args:
        urls: List of image URLs

    Returns:
        Tuple of (valid_urls, invalid_urls)
    """
    valid = []
    invalid = []

    for url in urls:
        if validate_url_format(url):
            # Check for image extensions
            url_lower = url.lower()
            if any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
                valid.append(url)
            else:
                # Could still be valid (CDN URLs without extensions)
                valid.append(url)
        else:
            invalid.append(url)

    return valid, invalid
