"""
Validation tier assignment.

Assigns items to validation tiers based on data quality:
- Tier 0: Lab report verified, highest quality
- Tier 1: Strong structured data from trusted source (no report)
- Tier 2: Weak labels, pretraining only
- Tier 3: Reject
"""

from dataclasses import dataclass, field
from typing import Optional

import structlog

from config.allowed_sites import get_site_config, is_recognized_lab
from scrapers.models import GemstoneItem, ValidationResult, ValidationTier
from validators.lab_reports import validate_lab_report_id
from validators.sanity import SanityChecker, SanityCheckResult

logger = structlog.get_logger(__name__)


# Trusted institutional sources (auction houses)
TRUSTED_INSTITUTIONAL_SOURCES = {
    "sothebys.com",
    "christies.com",
    "bonhams.com",
}


@dataclass
class TierDecision:
    """Details about tier assignment decision."""

    tier: ValidationTier
    confidence: float
    keep: bool
    reasons: list[str] = field(default_factory=list)
    factors: dict = field(default_factory=dict)


class TierAssigner:
    """
    Assigns validation tiers to gemstone items.

    Tier criteria:
    - Tier 0: lab_report_id present AND format valid AND sanity checks pass
    - Tier 1: No report but trusted institutional source + strong descriptive consistency
    - Tier 2: Weak labels, allowed only for pretraining/robustness
    - Tier 3: Reject
    """

    def __init__(self, sanity_checker: Optional[SanityChecker] = None):
        """
        Initialize the tier assigner.

        Args:
            sanity_checker: Optional custom sanity checker
        """
        self.sanity_checker = sanity_checker or SanityChecker()

    def _check_tier_0_eligibility(
        self, item: GemstoneItem, sanity_result: SanityCheckResult
    ) -> tuple[bool, list[str], float]:
        """
        Check if item qualifies for Tier 0 (lab report verified).

        Requirements:
        - Valid lab report ID present
        - Lab name recognized
        - Sanity checks pass
        """
        reasons = []
        factors = {}

        # Check lab report
        lab_validation = validate_lab_report_id(item.lab_name, item.lab_report_id)
        factors["lab_valid"] = lab_validation.is_valid

        if not lab_validation.is_valid:
            reasons.append(f"Lab report invalid: {lab_validation.message}")
            return False, reasons, 0.0

        if not is_recognized_lab(item.lab_name):
            reasons.append(f"Lab not recognized: {item.lab_name}")
            return False, reasons, 0.0

        # Check sanity
        if not sanity_result.passed:
            reasons.extend([f"Sanity check failed: {r}" for r in sanity_result.checks_failed])
            return False, reasons, 0.0

        # Calculate confidence based on data completeness
        confidence = 0.9  # Base confidence for tier 0

        # Boost for complete data
        if item.carat:
            confidence += 0.02
        if item.shape:
            confidence += 0.02
        if item.color_family:
            confidence += 0.02
        if len(item.image_urls) >= 4:
            confidence += 0.02
        if item.description_text and len(item.description_text) > 100:
            confidence += 0.02

        confidence = min(1.0, confidence)

        logger.info(
            "tier_0_eligible",
            item_id=item.item_id,
            lab=item.lab_name,
            report_id=item.lab_report_id,
            confidence=confidence,
        )

        return True, reasons, confidence

    def _check_tier_1_eligibility(
        self, item: GemstoneItem, sanity_result: SanityCheckResult
    ) -> tuple[bool, list[str], float]:
        """
        Check if item qualifies for Tier 1 (trusted institutional source).

        Requirements:
        - Source is trusted institutional (auction house)
        - Strong descriptive consistency
        - Sanity checks pass
        """
        reasons = []

        # Check if from trusted institutional source
        site_config = get_site_config(item.source_site)
        is_institutional = item.source_site in TRUSTED_INSTITUTIONAL_SOURCES

        if not is_institutional:
            reasons.append(f"Not from trusted institutional source: {item.source_site}")
            return False, reasons, 0.0

        # Check sanity (more lenient for auction houses)
        critical_failures = [
            f for f in sanity_result.checks_failed
            if "stone_type_unknown" in f or "url_invalid" in f
        ]

        if critical_failures:
            reasons.extend([f"Critical sanity failure: {r}" for r in critical_failures])
            return False, reasons, 0.0

        # Check descriptive consistency
        description_score = self._score_description_quality(item)
        if description_score < 0.5:
            reasons.append(f"Weak description quality: {description_score:.2f}")
            return False, reasons, 0.0

        # Calculate confidence
        confidence = 0.7  # Base confidence for tier 1

        # Boost for data completeness
        if item.carat:
            confidence += 0.05
        if item.shape:
            confidence += 0.05
        if item.color_family:
            confidence += 0.05
        if len(item.image_urls) >= 3:
            confidence += 0.05

        # Boost if lab is mentioned (even without ID)
        if item.lab_name:
            confidence += 0.05

        confidence = min(0.89, confidence)  # Cap below tier 0

        logger.info(
            "tier_1_eligible",
            item_id=item.item_id,
            source=item.source_site,
            confidence=confidence,
        )

        return True, reasons, confidence

    def _check_tier_2_eligibility(
        self, item: GemstoneItem, sanity_result: SanityCheckResult
    ) -> tuple[bool, list[str], float]:
        """
        Check if item qualifies for Tier 2 (weak labels, pretraining only).

        Requirements:
        - Stone type is identified
        - At least 1 image
        - Basic data integrity
        """
        reasons = []

        # Must have stone type
        if not item.stone_type_claimed or item.stone_type_claimed.lower() == "unknown":
            reasons.append("Stone type unknown")
            return False, reasons, 0.0

        # Must have at least one image
        if not item.image_urls:
            reasons.append("No images")
            return False, reasons, 0.0

        # Must have valid URL
        if "url_invalid" in str(sanity_result.checks_failed):
            reasons.append("Invalid URL")
            return False, reasons, 0.0

        # Calculate confidence (low for tier 2)
        confidence = 0.4  # Base confidence for tier 2

        # Small boosts for additional data
        if item.carat:
            confidence += 0.05
        if item.shape:
            confidence += 0.05
        if len(item.image_urls) >= 2:
            confidence += 0.1

        confidence = min(0.69, confidence)  # Cap below tier 1

        logger.info(
            "tier_2_eligible",
            item_id=item.item_id,
            confidence=confidence,
        )

        return True, reasons, confidence

    def _score_description_quality(self, item: GemstoneItem) -> float:
        """
        Score the quality of item description.

        Looks for:
        - Length
        - Presence of gemstone-related terms
        - Specificity
        """
        if not item.description_text:
            return 0.0

        score = 0.0
        text = item.description_text.lower()

        # Length factor
        length = len(item.description_text)
        if length > 500:
            score += 0.3
        elif length > 200:
            score += 0.2
        elif length > 50:
            score += 0.1

        # Gemstone terms
        gem_terms = [
            "carat", "ct", "diamond", "ruby", "sapphire", "emerald",
            "cut", "clarity", "color", "certificate", "natural",
            "untreated", "heated", "origin", "burma", "kashmir",
            "colombian", "brazilian", "cushion", "oval", "round",
        ]
        term_count = sum(1 for term in gem_terms if term in text)
        score += min(0.4, term_count * 0.05)

        # Specificity (numbers, measurements)
        import re
        numbers = len(re.findall(r"\d+\.?\d*", text))
        score += min(0.3, numbers * 0.03)

        return min(1.0, score)

    def assign_tier(self, item: GemstoneItem) -> ValidationResult:
        """
        Assign a validation tier to an item.

        Args:
            item: The item to validate

        Returns:
            ValidationResult with tier and details
        """
        # Run sanity checks
        sanity_result = self.sanity_checker.run_all_checks(item)

        all_reasons = []

        # Check Tier 0
        t0_eligible, t0_reasons, t0_confidence = self._check_tier_0_eligibility(
            item, sanity_result
        )
        all_reasons.extend(t0_reasons)

        if t0_eligible:
            return ValidationResult(
                tier=ValidationTier.TIER_0,
                confidence=t0_confidence,
                keep=True,
                reject_reasons=[],
                sanity_checks_passed=sanity_result.checks_passed,
                sanity_checks_failed=sanity_result.checks_failed,
            )

        # Check Tier 1
        t1_eligible, t1_reasons, t1_confidence = self._check_tier_1_eligibility(
            item, sanity_result
        )
        all_reasons.extend(t1_reasons)

        if t1_eligible:
            return ValidationResult(
                tier=ValidationTier.TIER_1,
                confidence=t1_confidence,
                keep=True,
                reject_reasons=t0_reasons,  # Show why not tier 0
                sanity_checks_passed=sanity_result.checks_passed,
                sanity_checks_failed=sanity_result.checks_failed,
            )

        # Check Tier 2
        t2_eligible, t2_reasons, t2_confidence = self._check_tier_2_eligibility(
            item, sanity_result
        )
        all_reasons.extend(t2_reasons)

        if t2_eligible:
            return ValidationResult(
                tier=ValidationTier.TIER_2,
                confidence=t2_confidence,
                keep=True,  # Keep for pretraining
                reject_reasons=t0_reasons + t1_reasons,
                sanity_checks_passed=sanity_result.checks_passed,
                sanity_checks_failed=sanity_result.checks_failed,
            )

        # Tier 3 - Reject
        logger.info(
            "item_rejected",
            item_id=item.item_id,
            reasons=all_reasons,
        )

        return ValidationResult(
            tier=ValidationTier.TIER_3,
            confidence=0.0,
            keep=False,
            reject_reasons=all_reasons,
            sanity_checks_passed=sanity_result.checks_passed,
            sanity_checks_failed=sanity_result.checks_failed,
        )


def assign_tier(item: GemstoneItem) -> ValidationResult:
    """
    Assign a validation tier to an item using default settings.

    Args:
        item: The item to validate

    Returns:
        ValidationResult
    """
    assigner = TierAssigner()
    return assigner.assign_tier(item)
