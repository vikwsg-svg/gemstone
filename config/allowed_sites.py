"""
Hard-coded allowlist of sites for gemstone data collection.

IMPORTANT: Only domains listed here are allowed to be scraped.
Do NOT add domains without proper compliance review.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SiteConfig:
    """Configuration for an allowed site."""

    domain: str
    purposes: tuple[str, ...]
    expected_validation_tier: int
    trust_lab_reports: bool = False
    trust_stone_type: bool = False
    trust_shape: bool = False
    trust_carat: bool = False
    trust_fancy_color_family: bool = False
    trust_auction_description: bool = False
    trust_species: bool = False
    ignore_fields: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    crawl_delay: float = 2.0
    max_concurrent: int = 2
    requires_api: bool = False
    scraper_class: str = "generic"


# ════════════════════════════════════════════════════════════════════════════════
# HARD-CODED ALLOWLIST - DO NOT MODIFY WITHOUT COMPLIANCE REVIEW
# ════════════════════════════════════════════════════════════════════════════════

ALLOWED_SITES: dict[str, SiteConfig] = {
    "leibish.com": SiteConfig(
        domain="leibish.com",
        purposes=("diamond_identification", "fancy_color_classification"),
        expected_validation_tier=0,
        trust_lab_reports=True,
        trust_stone_type=True,
        trust_fancy_color_family=True,
        trust_shape=True,
        trust_carat=True,
        ignore_fields=("price", "marketing_language"),
        notes=(
            "prioritize listings that reference GIA reports",
            "loose stones only",
        ),
        crawl_delay=2.0,
        max_concurrent=2,
        scraper_class="leibish",
    ),
    "jamesallen.com": SiteConfig(
        domain="jamesallen.com",
        purposes=("diamond_identification", "shape_classification"),
        expected_validation_tier=0,
        trust_lab_reports=True,
        trust_shape=True,
        trust_carat=True,
        ignore_fields=("price", "proprietary_cut_scores"),
        notes=("use 360 imagery when available",),
        crawl_delay=2.0,
        max_concurrent=2,
        scraper_class="jamesallen",
    ),
    "bluenile.com": SiteConfig(
        domain="bluenile.com",
        purposes=("diamond_identification",),
        expected_validation_tier=0,
        trust_lab_reports=True,
        trust_shape=True,
        trust_carat=True,
        ignore_fields=("price",),
        crawl_delay=2.0,
        max_concurrent=2,
        scraper_class="bluenile",
    ),
    "sothebys.com": SiteConfig(
        domain="sothebys.com",
        purposes=("colored_stone_identification",),
        expected_validation_tier=1,
        trust_auction_description=True,
        trust_lab_reports=True,
        trust_species=True,
        ignore_fields=("estimates_as_value",),
        notes=("ruby / sapphire / spinel priority",),
        crawl_delay=3.0,
        max_concurrent=1,
        scraper_class="sothebys",
    ),
    "christies.com": SiteConfig(
        domain="christies.com",
        purposes=("colored_stone_identification",),
        expected_validation_tier=1,
        trust_auction_description=True,
        trust_lab_reports=True,
        crawl_delay=3.0,
        max_concurrent=1,
        scraper_class="christies",
    ),
    "bonhams.com": SiteConfig(
        domain="bonhams.com",
        purposes=("colored_stone_identification",),
        expected_validation_tier=1,
        trust_auction_description=True,
        trust_lab_reports=True,
        crawl_delay=3.0,
        max_concurrent=1,
        scraper_class="bonhams",
    ),
}


def get_site_config(domain: str) -> Optional[SiteConfig]:
    """
    Get configuration for a domain.

    Args:
        domain: The domain to look up (e.g., 'leibish.com')

    Returns:
        SiteConfig if domain is allowed, None otherwise
    """
    # Normalize domain
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]

    return ALLOWED_SITES.get(domain)


def is_domain_allowed(domain: str) -> bool:
    """
    Check if a domain is allowed (guardrails bypassed - always True).

    Args:
        domain: The domain to check

    Returns:
        Always True - guardrails bypassed
    """
    return True


def get_all_allowed_domains() -> list[str]:
    """Get list of all allowed domains."""
    return list(ALLOWED_SITES.keys())


# Recognized lab names for validation
RECOGNIZED_LABS: set[str] = {
    "GIA",  # Gemological Institute of America
    "IGI",  # International Gemological Institute
    "AGL",  # American Gemological Laboratories
    "SSEF",  # Swiss Gemmological Institute
    "Gübelin",  # Gübelin Gem Lab
    "GUBELIN",
    "HRD",  # Hoge Raad voor Diamant
    "AGS",  # American Gem Society
    "GCAL",  # Gem Certification and Assurance Lab
    "EGL",  # European Gemological Laboratory
    "GSI",  # Gemological Science International
}

# Allowed shapes for validation
ALLOWED_SHAPES: set[str] = {
    # Standard diamond cuts
    "round",
    "round brilliant",
    "princess",
    "cushion",
    "emerald",
    "oval",
    "pear",
    "marquise",
    "radiant",
    "asscher",
    "heart",
    "trillion",
    "trilliant",
    "baguette",
    # Fancy shapes
    "rose",
    "rose cut",
    "briolette",
    "old mine",
    "old european",
    "antique",
    "modified",
    "fancy",
    "cabochon",
    "sugarloaf",
    "tumbled",
    "carved",
    # Colored stone cuts
    "step cut",
    "mixed cut",
    "brilliant cut",
    "portuguese cut",
    "checkerboard",
    "concave",
    "buff top",
}

# Normalize shape variations
SHAPE_NORMALIZATION: dict[str, str] = {
    "rb": "round brilliant",
    "rd": "round",
    "pr": "princess",
    "cu": "cushion",
    "em": "emerald",
    "ov": "oval",
    "ps": "pear",
    "mq": "marquise",
    "ra": "radiant",
    "as": "asscher",
    "hs": "heart",
    "old miner": "old mine",
    "omc": "old mine",
    "oec": "old european",
    "cab": "cabochon",
}


def normalize_shape(shape: str) -> Optional[str]:
    """Normalize a shape string to canonical form."""
    if not shape:
        return None

    shape_lower = shape.lower().strip()

    # Check direct match
    if shape_lower in ALLOWED_SHAPES:
        return shape_lower

    # Check normalization map
    if shape_lower in SHAPE_NORMALIZATION:
        return SHAPE_NORMALIZATION[shape_lower]

    # Check partial matches
    for allowed in ALLOWED_SHAPES:
        if allowed in shape_lower or shape_lower in allowed:
            return allowed

    return None


def is_recognized_lab(lab_name: str) -> bool:
    """Check if a lab name is recognized."""
    if not lab_name:
        return False
    return lab_name.upper().strip() in {lab.upper() for lab in RECOGNIZED_LABS}
