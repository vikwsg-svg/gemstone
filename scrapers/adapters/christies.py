"""
Christie's scraper adapter.

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


@register_scraper("christies.com")
class ChristiesScraper(BaseScraper):
    """
    Scraper for christies.com - colored stones from auctions.

    Trusted fields:
    - auction_description: true
    - lab_reports: true

    Tier 1 validation (trusted institutional source)
    """

    PRODUCT_URL_PATTERNS = [
        r"/lot/",
        r"/lotfinder/",
        r"/sale/.+/lot-",
        r"/auction/.+/lot",
    ]

    CATEGORY_URL_PATTERNS = [
        r"/auctions/",
        r"/sales/",
        r"/calendar/jewels",
        r"/jewelry",
        r"/magnificent-jewels",
    ]

    PRIORITY_STONES = {
        "ruby", "sapphire", "spinel", "emerald",
        "alexandrite", "padparadscha", "diamond",
    }

    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """Extract gemstone item from Christie's lot page."""
        soup = self.get_soup(html)

        # Extract title
        title = self._extract_title(soup)
        if not title:
            return None

        # Skip non-gemstone lots
        if not self._is_gemstone_lot(title):
            return None

        # Create base item
        item = self.create_base_item(url, title, compliance)

        # Extract description
        item.description_text = self._extract_description(soup)

        # Extract images
        item.image_urls = self._extract_images(soup, url)

        # Extract stone attributes
        combined = f"{title} {item.description_text}"
        item.stone_type_claimed = self._extract_stone_type(combined)
        item.shape = self.extract_shape(combined)
        item.color_family = self.extract_color_family(combined)
        item.carat = self.extract_carat(combined)

        # Extract lab info
        item.lab_name, item.lab_report_id = self._extract_lab_info(combined)
        item.has_lab_report = bool(item.lab_name)

        # Extract additional details
        item.extracted_attributes = self._extract_lot_details(soup)

        return item

    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """Discover lot URLs from Christie's pages."""
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
            if link_domain != "christies.com":
                continue

            canonical = self.canonicalize_url(full_url)
            if canonical in seen:
                continue
            seen.add(canonical)

            if self._is_skip_url(full_url):
                continue

            link_text = self.extract_text(link).lower()
            is_gem_link = any(stone in link_text for stone in self.PRIORITY_STONES)

            priority = 0
            if self._is_product_url(full_url):
                priority = 10 if is_gem_link else 5
            elif self._is_category_url(full_url):
                priority = 3

            if priority > 0:
                discovered.append(
                    DiscoveredURL(
                        url=full_url,
                        domain="christies.com",
                        source_type="crawl",
                        depth=current_depth + 1,
                        priority=priority,
                    )
                )

        return discovered

    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """Check if URL is a lot page."""
        return self._is_product_url(url)

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract lot title."""
        selectors = [
            "h1.chr-lot-header__title",
            ".lot-title h1",
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
            ".chr-lot-header__description",
            ".lot-description",
            ".essay-text",
            ".cataloguing",
            "[data-testid='lot-description']",
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
            ".chr-lot-header__image img",
            ".lot-image img",
            ".gallery img",
            "[data-testid='lot-image'] img",
            "img[src*='lot']",
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
                    full_url = self._get_high_res_url(full_url)
                    if full_url not in seen and self._is_valid_image(full_url):
                        seen.add(full_url)
                        images.append(full_url)

        return images

    def _extract_stone_type(self, text: str) -> str:
        """Extract stone type."""
        text_lower = text.lower()
        for stone in self.PRIORITY_STONES:
            if stone in text_lower:
                return stone
        return self.extract_stone_type(text, "") or "unknown"

    def _extract_lab_info(
        self, text: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract lab information."""
        lab_patterns = [
            (r"(?:accompanied by|with)\s+(?:a\s+)?(\w+)\s+(?:report|certificate)", "lab"),
            (r"(\w+)\s+(?:Report|Certificate)\s*(?:No\.?|#)?\s*[:.]?\s*(\d+)", "full"),
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

        return self.extract_lab_info(text)

    def _extract_lot_details(self, soup: BeautifulSoup) -> dict:
        """Extract lot details."""
        details = {}

        containers = soup.select(
            ".lot-details, .chr-lot-header__details, .lot-info"
        )

        for container in containers:
            for row in container.select("tr, .detail-row"):
                cells = row.find_all(["th", "td", ".label", ".value"])
                if len(cells) >= 2:
                    key = self.extract_text(cells[0]).lower().strip(":")
                    value = self.extract_text(cells[1])
                    if key and value:
                        details[key] = value

        return details

    def _is_gemstone_lot(self, title: str) -> bool:
        """Check if lot is a gemstone."""
        title_lower = title.lower()
        keywords = [
            "ruby", "sapphire", "emerald", "diamond",
            "spinel", "alexandrite", "carat", " ct ",
            "gemstone", "gem", "padparadscha", "paraiba",
        ]
        return any(kw in title_lower for kw in keywords)

    def _is_product_url(self, url: str) -> bool:
        """Check if URL is a lot page."""
        return any(re.search(p, url.lower()) for p in self.PRODUCT_URL_PATTERNS)

    def _is_category_url(self, url: str) -> bool:
        """Check if URL is a category page."""
        return any(re.search(p, url.lower()) for p in self.CATEGORY_URL_PATTERNS)

    def _is_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped."""
        skip = [
            "/cart", "/checkout", "/account", "/login",
            "/sell", "/consign", "/about", "/contact",
            "/news", "/press", "/services", ".pdf",
        ]
        return any(s in url.lower() for s in skip)

    def _is_valid_image(self, url: str) -> bool:
        """Check if URL is a valid image."""
        url_lower = url.lower()
        skip = ["thumb", "icon", "logo", "banner", "tiny"]
        if any(s in url_lower for s in skip):
            return False
        return any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])

    def _get_high_res_url(self, url: str) -> str:
        """Get high-resolution version."""
        url = re.sub(r"_\d+x\d+", "", url)
        url = re.sub(r"\?.*$", "", url)
        return url
