"""
Generic scraper that provides fallback extraction for any allowed domain.

This scraper uses heuristics to extract data when no site-specific adapter
is available. It's less reliable than site-specific adapters but provides
baseline functionality.
"""

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.models import ComplianceInfo, DiscoveredURL, GemstoneItem


class GenericScraper(BaseScraper):
    """
    Generic scraper using heuristics for data extraction.

    This scraper attempts to extract gemstone data from any page using
    common HTML patterns and text analysis. Results should be treated
    with lower confidence than site-specific scrapers.
    """

    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """Extract gemstone item using generic heuristics."""
        soup = self.get_soup(html)

        # Extract title
        title = self._extract_title(soup)
        if not title:
            return None

        # Create base item
        item = self.create_base_item(url, title, compliance)

        # Extract description
        item.description_text = self._extract_description(soup)

        # Extract images
        item.image_urls = self.extract_images(soup, url)

        # Extract stone attributes from title and description
        combined_text = f"{title} {item.description_text}"

        item.stone_type_claimed = (
            self.extract_stone_type(combined_text, title) or "unknown"
        )
        item.shape = self.extract_shape(combined_text)
        item.carat = self.extract_carat(combined_text)
        item.color_family = self.extract_color_family(combined_text)

        # Extract lab info
        lab_name, report_id = self.extract_lab_info(combined_text)
        item.lab_name = lab_name
        item.lab_report_id = report_id
        item.has_lab_report = bool(lab_name)

        # Extract any additional structured data
        item.extracted_attributes = self._extract_structured_data(soup)

        return item

    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """Discover URLs using generic link analysis."""
        soup = self.get_soup(html)
        discovered = []
        seen = set()

        # Find all links
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href:
                continue

            # Resolve relative URL
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            # Skip if not same domain
            link_domain = parsed.netloc.lower()
            if link_domain.startswith("www."):
                link_domain = link_domain[4:]
            if link_domain != self.domain:
                continue

            # Skip non-http URLs
            if parsed.scheme not in ("http", "https"):
                continue

            # Skip already seen
            canonical = self.canonicalize_url(full_url)
            if canonical in seen:
                continue
            seen.add(canonical)

            # Skip common non-product pages
            if self._is_skip_url(full_url):
                continue

            # Determine if this might be a listing page
            priority = 0
            if self._looks_like_listing(full_url, link):
                priority = 10

            discovered.append(
                DiscoveredURL(
                    url=full_url,
                    domain=self.domain,
                    source_type="crawl",
                    depth=current_depth + 1,
                    priority=priority,
                )
            )

        return discovered

    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """Check if URL appears to be a product listing page."""
        # Check URL patterns
        url_lower = url.lower()

        listing_indicators = [
            "/product/",
            "/item/",
            "/stone/",
            "/diamond/",
            "/gemstone/",
            "/lot/",
            "/jewelry/",
            "-diamond",
            "-ct-",
            "-carat",
        ]

        if any(ind in url_lower for ind in listing_indicators):
            return True

        # Check HTML if provided
        if html:
            soup = self.get_soup(html)

            # Look for product schema
            if soup.find(attrs={"itemtype": re.compile(r"Product", re.I)}):
                return True

            # Look for common product page elements
            product_selectors = [
                ".product-detail",
                ".product-page",
                "#product-info",
                "[data-product-id]",
                ".item-detail",
            ]
            for selector in product_selectors:
                if soup.select_one(selector):
                    return True

        return False

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract page title."""
        # Try common title selectors
        selectors = [
            "h1.product-title",
            "h1.item-title",
            ".product-name h1",
            "[itemprop='name']",
            "h1",
            "title",
        ]

        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                text = self.extract_text(elem)
                if text and len(text) > 5:
                    return text

        return ""

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """Extract product description."""
        selectors = [
            ".product-description",
            ".item-description",
            "[itemprop='description']",
            ".description",
            "#description",
            ".details",
        ]

        texts = []
        for selector in selectors:
            for elem in soup.select(selector):
                text = self.extract_text(elem)
                if text and len(text) > 20:
                    texts.append(text)

        return " ".join(texts)

    def _extract_structured_data(self, soup: BeautifulSoup) -> dict:
        """Extract any structured data (JSON-LD, microdata, etc.)."""
        data = {}

        # Look for JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json

                json_data = json.loads(script.string)
                if isinstance(json_data, dict):
                    if json_data.get("@type") == "Product":
                        data["json_ld"] = json_data
            except (json.JSONDecodeError, TypeError):
                pass

        # Look for data attributes
        for elem in soup.find_all(attrs={"data-product-info": True}):
            try:
                import json

                info = json.loads(elem.get("data-product-info"))
                data["product_info"] = info
            except (json.JSONDecodeError, TypeError):
                pass

        return data

    def _is_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped."""
        url_lower = url.lower()

        skip_patterns = [
            "/login",
            "/signin",
            "/signup",
            "/register",
            "/cart",
            "/checkout",
            "/account",
            "/wishlist",
            "/contact",
            "/about",
            "/faq",
            "/help",
            "/privacy",
            "/terms",
            "/policy",
            "/shipping",
            "/returns",
            "/blog/",
            "/news/",
            "/press/",
            ".pdf",
            ".jpg",
            ".png",
            ".gif",
            "javascript:",
            "mailto:",
            "tel:",
            "#",
        ]

        return any(pattern in url_lower for pattern in skip_patterns)

    def _looks_like_listing(self, url: str, link_elem) -> bool:
        """Check if a link looks like it goes to a listing page."""
        url_lower = url.lower()

        # URL indicators
        listing_url_patterns = [
            "/product/",
            "/item/",
            "/diamond/",
            "/gemstone/",
            "/stone/",
            "/lot/",
            r"/\d+\.\d+.*carat",
            r"/\d+ct",
        ]

        for pattern in listing_url_patterns:
            if re.search(pattern, url_lower):
                return True

        # Link text indicators
        link_text = self.extract_text(link_elem).lower()
        text_indicators = ["carat", "diamond", "ruby", "sapphire", "emerald"]
        if any(ind in link_text for ind in text_indicators):
            return True

        return False
