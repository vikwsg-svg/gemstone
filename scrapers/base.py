"""
Base scraper class that all site adapters must inherit from.

This provides the common interface and helper methods for all scrapers.
"""

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from config.allowed_sites import (
    ALLOWED_SHAPES,
    RECOGNIZED_LABS,
    SiteConfig,
    get_site_config,
    normalize_shape,
)
from scrapers.models import (
    ComplianceInfo,
    DiscoveredURL,
    GemstoneItem,
    ScrapedImage,
)


class BaseScraper(ABC):
    """
    Abstract base class for site-specific scrapers.

    Each site adapter must implement:
    - extract_item(): Parse HTML and extract gemstone item data
    - discover_urls(): Find more URLs to crawl from a page
    """

    # Patterns for extracting lab report IDs
    LAB_REPORT_PATTERNS = {
        "GIA": [
            r"GIA[:\s#]*(\d{10})",
            r"(?:GIA|gia)\s*(?:Report|Certificate|Cert)[:\s#]*(\d{10})",
            r"(?:Report|Certificate)\s*(?:No|Number|#)?[:\s]*(\d{10})",
        ],
        "IGI": [
            r"IGI[:\s#]*(\d{9})",
            r"(?:IGI|igi)\s*(?:Report|Certificate)[:\s#]*(\d{9})",
        ],
        "AGL": [
            r"AGL[:\s#]*(\d{7,8})",
            r"(?:AGL|American Gemological)[:\s#]*(\d{7,8})",
        ],
        "SSEF": [
            r"SSEF[:\s#]*(\d{5,8})",
        ],
        "Gübelin": [
            r"(?:Gübelin|Gubelin|GUBELIN)[:\s#]*(\d{7,10})",
        ],
    }

    # Common stone types to look for
    STONE_TYPES = {
        "diamond",
        "ruby",
        "sapphire",
        "emerald",
        "alexandrite",
        "spinel",
        "tourmaline",
        "aquamarine",
        "tanzanite",
        "opal",
        "garnet",
        "topaz",
        "peridot",
        "amethyst",
        "citrine",
        "morganite",
        "kunzite",
        "tsavorite",
        "demantoid",
        "paraiba",
        "padparadscha",
    }

    # Color families
    COLOR_FAMILIES = {
        "colorless",
        "white",
        "yellow",
        "orange",
        "pink",
        "red",
        "purple",
        "violet",
        "blue",
        "green",
        "brown",
        "gray",
        "grey",
        "black",
        "fancy",
        "champagne",
        "cognac",
    }

    def __init__(self, domain: str):
        """
        Initialize the scraper for a specific domain.

        Args:
            domain: The domain this scraper handles (must be in allowlist)
        """
        self.domain = domain
        self.site_config = get_site_config(domain)
        if not self.site_config:
            raise ValueError(f"Domain {domain} is not in the allowlist")

    @abstractmethod
    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """
        Extract a gemstone item from HTML content.

        Args:
            html: Raw HTML content of the page
            url: URL of the page
            compliance: Compliance information for the request

        Returns:
            GemstoneItem if extraction successful, None otherwise
        """
        pass

    @abstractmethod
    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """
        Discover new URLs to crawl from a page.

        Args:
            html: Raw HTML content
            base_url: Base URL for resolving relative links
            current_depth: Current crawl depth

        Returns:
            List of discovered URLs
        """
        pass

    @abstractmethod
    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """
        Check if a URL/page is a product listing page.

        Args:
            url: The URL to check
            html: Optional HTML content for additional checks

        Returns:
            True if this is a listing page
        """
        pass

    def get_soup(self, html: str) -> BeautifulSoup:
        """Create a BeautifulSoup object from HTML."""
        return BeautifulSoup(html, "lxml")

    def extract_text(self, element: Any) -> str:
        """Extract and clean text from a BeautifulSoup element."""
        if element is None:
            return ""
        text = element.get_text(separator=" ", strip=True)
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def extract_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """
        Extract image URLs from the page.

        Args:
            soup: BeautifulSoup object
            base_url: Base URL for resolving relative links

        Returns:
            List of absolute image URLs
        """
        images = []
        seen = set()

        # Common image selectors for product pages
        selectors = [
            "img[src*='product']",
            "img[src*='diamond']",
            "img[src*='gemstone']",
            "img[src*='stone']",
            ".product-image img",
            ".gallery img",
            "[data-zoom-image]",
            "[data-large-image]",
            ".main-image img",
            ".thumbnail img",
        ]

        for selector in selectors:
            for img in soup.select(selector):
                # Try various image source attributes
                src = (
                    img.get("data-zoom-image")
                    or img.get("data-large-image")
                    or img.get("data-src")
                    or img.get("src")
                )
                if src:
                    # Resolve relative URLs
                    full_url = urljoin(base_url, src)
                    # Filter out tiny images, icons, etc.
                    if self._is_valid_image_url(full_url) and full_url not in seen:
                        seen.add(full_url)
                        images.append(full_url)

        # Also look for images in data attributes
        for elem in soup.find_all(attrs={"data-image": True}):
            src = elem.get("data-image")
            if src:
                full_url = urljoin(base_url, src)
                if self._is_valid_image_url(full_url) and full_url not in seen:
                    seen.add(full_url)
                    images.append(full_url)

        return images

    def _is_valid_image_url(self, url: str) -> bool:
        """Check if URL looks like a valid product image."""
        url_lower = url.lower()

        # Skip common non-product images
        skip_patterns = [
            "icon",
            "logo",
            "banner",
            "placeholder",
            "loading",
            "spinner",
            "avatar",
            "social",
            "facebook",
            "twitter",
            "instagram",
            "pixel",
            "tracking",
            "1x1",
            "spacer",
        ]
        if any(pattern in url_lower for pattern in skip_patterns):
            return False

        # Must have image extension or be from image CDN
        image_indicators = [".jpg", ".jpeg", ".png", ".webp", "image/", "/images/"]
        return any(ind in url_lower for ind in image_indicators)

    def extract_lab_info(
        self, text: str
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Extract lab name and report ID from text.

        Args:
            text: Text to search for lab information

        Returns:
            Tuple of (lab_name, report_id)
        """
        text_upper = text.upper()

        for lab_name, patterns in self.LAB_REPORT_PATTERNS.items():
            # Check if lab is mentioned
            if lab_name.upper() in text_upper:
                # Try to extract report ID
                for pattern in patterns:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        return lab_name, match.group(1)
                # Lab mentioned but no ID found
                return lab_name, None

        return None, None

    def extract_carat(self, text: str) -> Optional[float]:
        """
        Extract carat weight from text.

        Args:
            text: Text to search for carat information

        Returns:
            Carat weight as float, or None
        """
        # Various carat patterns
        patterns = [
            r"(\d+\.?\d*)\s*(?:ct|carat|carats|cts)",
            r"(?:carat|weight)[:\s]*(\d+\.?\d*)",
            r"(\d+\.?\d*)\s*(?:total\s+)?carat",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    carat = float(match.group(1))
                    # Sanity check
                    if 0.001 <= carat <= 500:
                        return carat
                except ValueError:
                    continue

        return None

    def extract_stone_type(self, text: str, title: str = "") -> Optional[str]:
        """
        Extract stone type from text.

        Args:
            text: Description text
            title: Title text (often more reliable)

        Returns:
            Stone type string or None
        """
        combined = f"{title} {text}".lower()

        for stone in self.STONE_TYPES:
            if stone in combined:
                return stone

        return None

    def extract_shape(self, text: str) -> Optional[str]:
        """
        Extract and normalize shape from text.

        Args:
            text: Text to search for shape

        Returns:
            Normalized shape string or None
        """
        text_lower = text.lower()

        for shape in ALLOWED_SHAPES:
            if shape in text_lower:
                return shape

        # Try normalization for abbreviations
        return normalize_shape(text)

    def extract_color_family(self, text: str) -> Optional[str]:
        """
        Extract color family from text.

        Args:
            text: Text to search for color

        Returns:
            Color family string or None
        """
        text_lower = text.lower()

        for color in self.COLOR_FAMILIES:
            if color in text_lower:
                return color

        return None

    def canonicalize_url(self, url: str) -> str:
        """
        Canonicalize URL for deduplication.

        Args:
            url: URL to canonicalize

        Returns:
            Canonical URL string
        """
        parsed = urlparse(url)

        # Normalize scheme
        scheme = "https"

        # Normalize host (lowercase, remove www)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]

        # Normalize path (remove trailing slash, lowercase)
        path = parsed.path.rstrip("/").lower()

        # Remove common tracking parameters
        # Keep only essential query params
        # This is site-specific, so subclasses may override

        return f"{scheme}://{host}{path}"

    def create_base_item(
        self,
        url: str,
        title: str,
        compliance: ComplianceInfo,
    ) -> GemstoneItem:
        """
        Create a base GemstoneItem with common fields populated.

        Args:
            url: Source URL
            title: Page title
            compliance: Compliance information

        Returns:
            Partially populated GemstoneItem
        """
        return GemstoneItem(
            source_site=self.domain,
            source_url=url,
            scraped_at_utc=datetime.utcnow(),
            title=title,
            stone_type_claimed="unknown",
            compliance=compliance,
            canonical_url=self.canonicalize_url(url),
        )
