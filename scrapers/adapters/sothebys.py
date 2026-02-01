"""
Sotheby's scraper adapter.

Specializes in colored stones from auction lots.
Expected validation tier: 1 (trusted institutional source)
"""

import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.models import ComplianceInfo, DiscoveredURL, GemstoneItem
from scrapers.registry import register_scraper


@register_scraper("sothebys.com")
class SothebysScraper(BaseScraper):
    """
    Scraper for sothebys.com - colored stones from auctions.

    Trusted fields:
    - auction_description: true
    - lab_reports: true
    - species: true

    Ignored:
    - estimates_as_value

    Notes:
    - Ruby / sapphire / spinel priority
    - Tier 1 validation (trusted institutional source)
    """

    PRODUCT_URL_PATTERNS = [
        r"/buy/auction/",
        r"/lot/",
        r"/auctions/.+/lot-",
    ]

    CATEGORY_URL_PATTERNS = [
        r"/auctions/",
        r"/buy/jewels",
        r"/jewelry",
        r"/magnificent-jewels",
    ]

    # Priority stone types for Sotheby's
    PRIORITY_STONES = {
        "ruby",
        "sapphire",
        "spinel",
        "emerald",
        "alexandrite",
        "padparadscha",
    }

    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """Extract gemstone item from Sotheby's lot page."""
        soup = self.get_soup(html)

        # Extract lot title
        title = self._extract_title(soup)
        if not title:
            return None

        # Skip if not a gemstone lot
        if not self._is_gemstone_lot(title):
            return None

        # Create base item
        item = self.create_base_item(url, title, compliance)

        # Extract lot description (very detailed for auction houses)
        item.description_text = self._extract_description(soup)

        # Extract images
        item.image_urls = self._extract_images(soup, url)

        # Extract stone type from title and description
        combined = f"{title} {item.description_text}"
        item.stone_type_claimed = self._extract_stone_type(combined)

        # Extract shape
        item.shape = self.extract_shape(combined)

        # Extract color family
        item.color_family = self.extract_color_family(combined)

        # Extract carat
        item.carat = self.extract_carat(combined)

        # Extract lab information (auction descriptions often include lab reports)
        item.lab_name, item.lab_report_id = self._extract_lab_info(combined)
        item.has_lab_report = bool(item.lab_name)

        # Extract additional attributes
        item.extracted_attributes = self._extract_lot_details(soup)

        return item

    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """Discover lot URLs from Sotheby's pages."""
        soup = self.get_soup(html)
        discovered = []
        seen = set()

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href:
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            link_domain = parsed.netloc.lower()
            if link_domain.startswith("www."):
                link_domain = link_domain[4:]
            if link_domain != "sothebys.com":
                continue

            canonical = self.canonicalize_url(full_url)
            if canonical in seen:
                continue
            seen.add(canonical)

            if self._is_skip_url(full_url):
                continue

            # Check link text for gemstone keywords
            link_text = self.extract_text(link).lower()
            is_gem_link = any(
                stone in link_text for stone in self.PRIORITY_STONES
            ) or any(
                kw in link_text
                for kw in ["carat", "ct", "jewel", "gem", "diamond"]
            )

            priority = 0
            if self._is_product_url(full_url):
                priority = 10 if is_gem_link else 5
            elif self._is_category_url(full_url):
                priority = 3

            if priority > 0:
                discovered.append(
                    DiscoveredURL(
                        url=full_url,
                        domain="sothebys.com",
                        source_type="crawl",
                        depth=current_depth + 1,
                        priority=priority,
                    )
                )

        return discovered

    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """Check if URL is a lot page."""
        if not self._is_product_url(url):
            return False

        if html:
            soup = self.get_soup(html)
            title = self._extract_title(soup)
            return self._is_gemstone_lot(title) if title else False

        return True

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract lot title."""
        selectors = [
            "h1.lot-title",
            ".lot-page__title",
            "[data-testid='lot-title']",
            ".lot-name",
            "h1",
        ]

        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                text = self.extract_text(elem)
                if text:
                    return text
        return ""

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """Extract lot description."""
        selectors = [
            ".lot-description",
            ".lot-page__description",
            "[data-testid='lot-description']",
            ".lot-details",
            ".cataloguing",
        ]

        texts = []
        for selector in selectors:
            for elem in soup.select(selector):
                text = self.extract_text(elem)
                if text and len(text) > 20:
                    texts.append(text)

        return " ".join(texts)

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract lot images."""
        images = []
        seen = set()

        selectors = [
            ".lot-image img",
            ".lot-page__image img",
            "[data-testid='lot-image'] img",
            ".gallery-image img",
            "img[src*='lot']",
            "img[src*='auction']",
        ]

        for selector in selectors:
            for elem in soup.select(selector):
                src = (
                    elem.get("data-zoom")
                    or elem.get("data-large")
                    or elem.get("data-src")
                    or elem.get("src")
                )
                if src:
                    full_url = urljoin(base_url, src)
                    # Get high-res version
                    full_url = self._get_high_res_url(full_url)
                    if full_url not in seen and self._is_valid_image(full_url):
                        seen.add(full_url)
                        images.append(full_url)

        return images

    def _extract_stone_type(self, text: str) -> str:
        """Extract stone type, prioritizing specific stones."""
        text_lower = text.lower()

        # Check priority stones first
        for stone in self.PRIORITY_STONES:
            if stone in text_lower:
                return stone

        # Fall back to general extraction
        return self.extract_stone_type(text, "") or "unknown"

    def _extract_lab_info(
        self, text: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract lab information from auction description."""
        # Auction descriptions often mention labs explicitly
        lab_patterns = [
            (r"(?:accompanied by|with)\s+(?:a\s+)?(\w+)\s+(?:report|certificate)", "lab"),
            (r"(\w+)\s+(?:Report|Certificate)\s*(?:No\.?|Number|#)?\s*[:.]?\s*(\d+)", "full"),
            (r"(GIA|IGI|AGL|SSEF|Gübelin|GUBELIN)\s*[:.]?\s*(\d+)", "full"),
        ]

        for pattern, ptype in lab_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                if ptype == "lab":
                    lab_name = match.group(1).upper()
                    if lab_name in {"GIA", "IGI", "AGL", "SSEF", "GUBELIN"}:
                        return lab_name, None
                elif ptype == "full":
                    lab_name = match.group(1).upper()
                    report_id = match.group(2) if len(match.groups()) > 1 else None
                    return lab_name, report_id

        # Fall back to base extraction
        return self.extract_lab_info(text)

    def _extract_lot_details(self, soup: BeautifulSoup) -> dict:
        """Extract lot details."""
        details = {}

        # Look for lot info sections
        containers = soup.select(
            ".lot-info, .lot-details, .lot-page__details"
        )

        for container in containers:
            for row in container.select("tr, .detail-row, dt"):
                if row.name == "dt":
                    dd = row.find_next_sibling("dd")
                    if dd:
                        key = self.extract_text(row).lower().strip(":")
                        value = self.extract_text(dd)
                        if key and value:
                            details[key] = value
                else:
                    cells = row.find_all(["th", "td", ".label", ".value"])
                    if len(cells) >= 2:
                        key = self.extract_text(cells[0]).lower().strip(":")
                        value = self.extract_text(cells[1])
                        if key and value:
                            details[key] = value

        return details

    def _is_gemstone_lot(self, title: str) -> bool:
        """Check if lot title indicates a gemstone."""
        title_lower = title.lower()

        gemstone_keywords = [
            "ruby", "sapphire", "emerald", "diamond",
            "spinel", "alexandrite", "tourmaline",
            "aquamarine", "tanzanite", "opal",
            "carat", " ct ", "gemstone", "gem",
            "padparadscha", "paraiba",
        ]

        return any(kw in title_lower for kw in gemstone_keywords)

    def _is_product_url(self, url: str) -> bool:
        """Check if URL is a lot page."""
        url_lower = url.lower()
        return any(re.search(p, url_lower) for p in self.PRODUCT_URL_PATTERNS)

    def _is_category_url(self, url: str) -> bool:
        """Check if URL is a category page."""
        url_lower = url.lower()
        return any(re.search(p, url_lower) for p in self.CATEGORY_URL_PATTERNS)

    def _is_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped."""
        url_lower = url.lower()
        skip = [
            "/cart", "/checkout", "/account", "/login",
            "/sell", "/consign", "/about", "/contact",
            "/news", "/press", "/services",
            ".pdf", "javascript:", "#",
        ]
        return any(s in url_lower for s in skip)

    def _is_valid_image(self, url: str) -> bool:
        """Check if URL is a valid lot image."""
        url_lower = url.lower()
        skip = ["thumb", "icon", "logo", "banner", "tiny"]
        if any(s in url_lower for s in skip):
            return False
        return any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])

    def _get_high_res_url(self, url: str) -> str:
        """Try to get high-resolution version of image URL."""
        # Common patterns for size parameters
        url = re.sub(r"_\d+x\d+", "", url)
        url = re.sub(r"\?.*$", "", url)
        return url
