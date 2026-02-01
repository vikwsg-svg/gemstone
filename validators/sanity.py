"""
Sanity checks for gemstone items.

Validates that item data is within plausible bounds.
"""

from dataclasses import dataclass, field
from typing import Optional

import structlog

from config.allowed_sites import ALLOWED_SHAPES, normalize_shape
from config.settings import get_settings
from scrapers.models import GemstoneItem, ScrapedImage

logger = structlog.get_logger(__name__)


@dataclass
class SanityCheckResult:
    """Result of sanity checks."""

    passed: bool
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SanityChecker:
    """
    Sanity checker for gemstone items.

    Validates:
    - Image count and resolution
    - Carat bounds
    - Shape validity
    - Data consistency
    """

    def __init__(
        self,
        min_image_count: Optional[int] = None,
        min_image_resolution: Optional[int] = None,
        min_carat: Optional[float] = None,
        max_carat: Optional[float] = None,
    ):
        """
        Initialize the sanity checker.

        Args:
            min_image_count: Minimum number of images required
            min_image_resolution: Minimum image resolution (width and height)
            min_carat: Minimum carat value
            max_carat: Maximum carat value
        """
        settings = get_settings()

        self.min_image_count = min_image_count or settings.min_image_count
        self.min_image_resolution = min_image_resolution or settings.min_image_resolution
        self.min_carat = min_carat or settings.min_carat
        self.max_carat = max_carat or settings.max_carat

    def check_image_count(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that item has minimum number of images.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        image_count = len(item.image_urls)

        if image_count >= self.min_image_count:
            return True, f"image_count_ok ({image_count} >= {self.min_image_count})"
        else:
            return False, f"image_count_low ({image_count} < {self.min_image_count})"

    def check_image_resolution(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that downloaded images meet resolution requirements.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        if not item.downloaded_images:
            # No downloaded images yet - pass with warning
            return True, "no_downloaded_images_yet"

        valid_images = 0
        for img in item.downloaded_images:
            if img.download_status == "success" and img.width and img.height:
                if img.width >= self.min_image_resolution and img.height >= self.min_image_resolution:
                    valid_images += 1

        if valid_images >= self.min_image_count:
            return True, f"image_resolution_ok ({valid_images} valid)"
        else:
            return False, f"image_resolution_low ({valid_images} < {self.min_image_count} valid)"

    def check_carat_bounds(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that carat is within plausible bounds.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        if item.carat is None:
            # Carat not provided - not a failure
            return True, "carat_not_provided"

        if self.min_carat <= item.carat <= self.max_carat:
            return True, f"carat_ok ({item.carat})"
        else:
            return False, f"carat_out_of_bounds ({item.carat} not in [{self.min_carat}, {self.max_carat}])"

    def check_shape_valid(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that shape is in the allowed set.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        if item.shape is None:
            # Shape not provided - not a failure
            return True, "shape_not_provided"

        normalized = normalize_shape(item.shape)
        if normalized:
            return True, f"shape_valid ({normalized})"
        else:
            # Unknown shape - warning but not failure
            return True, f"shape_unknown ({item.shape})"

    def check_stone_type_not_unknown(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that stone type is identified.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        if item.stone_type_claimed and item.stone_type_claimed.lower() != "unknown":
            return True, f"stone_type_identified ({item.stone_type_claimed})"
        else:
            return False, "stone_type_unknown"

    def check_title_length(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that title is meaningful.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        if item.title and len(item.title.strip()) >= 10:
            return True, "title_ok"
        else:
            return False, f"title_too_short ({len(item.title) if item.title else 0} chars)"

    def check_url_valid(self, item: GemstoneItem) -> tuple[bool, str]:
        """
        Check that source URL is valid.

        Args:
            item: The item to check

        Returns:
            Tuple of (passed, message)
        """
        if item.source_url and item.source_url.startswith(("http://", "https://")):
            return True, "url_valid"
        else:
            return False, "url_invalid"

    def run_all_checks(self, item: GemstoneItem) -> SanityCheckResult:
        """
        Run all sanity checks on an item.

        Args:
            item: The item to check

        Returns:
            SanityCheckResult with all check results
        """
        result = SanityCheckResult(passed=True)

        checks = [
            self.check_image_count,
            self.check_carat_bounds,
            self.check_shape_valid,
            self.check_stone_type_not_unknown,
            self.check_title_length,
            self.check_url_valid,
        ]

        for check in checks:
            passed, message = check(item)
            if passed:
                result.checks_passed.append(message)
            else:
                result.checks_failed.append(message)
                result.passed = False

        # Image resolution is a warning if images not yet downloaded
        res_passed, res_message = self.check_image_resolution(item)
        if res_passed:
            result.checks_passed.append(res_message)
            if "no_downloaded" in res_message:
                result.warnings.append("Images not yet downloaded for resolution check")
        else:
            result.checks_failed.append(res_message)
            result.passed = False

        logger.debug(
            "sanity_checks_complete",
            item_id=item.item_id,
            passed=result.passed,
            passed_count=len(result.checks_passed),
            failed_count=len(result.checks_failed),
        )

        return result


# Convenience function
def run_sanity_checks(item: GemstoneItem) -> SanityCheckResult:
    """
    Run sanity checks on an item using default settings.

    Args:
        item: The item to check

    Returns:
        SanityCheckResult
    """
    checker = SanityChecker()
    return checker.run_all_checks(item)
