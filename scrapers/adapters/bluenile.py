"""
BlueNile.com scraper adapter.

Specializes in certified diamonds.
Expected validation tier: 0 (lab report verified)
"""

import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.models import ComplianceInfo, DiscoveredURL, GemstoneItem
from scrapers.registry import register_scraper


@register_scraper("bluenile.com")
class BlueNileScraper(BaseScraper):
    """
    Scraper for bluenile.com - certified diamonds.

    Trusted fields:
    - lab_reports: true
    - shape: true
    - carat: true

    Ignored:
    - price
    """

    PRODUCT_URL_PATTERNS = [
        r"/diamond-details/",
        r"/diamonds/[a-zA-Z]+/LD",
        r"sku=LD",
        r"/build-your-own-ring/diamond-details",
    ]

    CATEGORY_URL_PATTERNS = [
        r"/diamonds/",
        r"/round-diamonds",
        r"/princess-diamonds",
        r"/cushion-diamonds",
        r"/oval-diamonds",
        r"/emerald-diamonds",
    ]

    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """Extract diamond item from Blue Nile product page."""
        soup = self.get_soup(html)

        # Extract from JSON data first (Blue Nile uses a lot of JS rendering)
        json_data = self._extract_product_data(soup)

        # Extract title
        title = self._extract_title(soup, json_data)
        if not title:
            return None

        # Create base item
        item = self.create_base_item(url, title, compliance)
        item.stone_type_claimed = "diamond"

        # Populate from JSON if available
        if json_data:
            item = self._populate_from_json(item, json_data)

        # Extract description
        desc_elem = soup.select_one(
            ".product-description, [itemprop='description'], .diamond-description"
        )
        if desc_elem:
            item.description_text = self.extract_text(desc_elem)

        # Extract images
        item.image_urls = self._extract_images(soup, url)

        # Extract specifications
        specs = self._extract_specifications(soup)
        item.extracted_attributes.update(specs)

        # Fill in missing fields from specs
        if not item.shape:
            item.shape = self._get_shape(specs, title)
        if not item.carat:
            item.carat = self._get_carat(specs, title)
        if not item.lab_name:
            item.lab_name, item.lab_report_id = self._get_lab_info(specs, title)
            item.has_lab_report = bool(item.lab_name)

        return item

    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """Discover diamond URLs from Blue Nile pages."""
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
            if link_domain != "bluenile.com":
                continue

            canonical = self.canonicalize_url(full_url)
            if canonical in seen:
                continue
            seen.add(canonical)

            if self._is_skip_url(full_url):
                continue

            priority = 0
            if self._is_product_url(full_url):
                priority = 10
            elif self._is_category_url(full_url):
                priority = 5

            if priority > 0:
                discovered.append(
                    DiscoveredURL(
                        url=full_url,
                        domain="bluenile.com",
                        source_type="crawl",
                        depth=current_depth + 1,
                        priority=priority,
                    )
                )

        return discovered

    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """Check if URL is a product page."""
        return self._is_product_url(url)

    def _extract_product_data(self, soup: BeautifulSoup) -> Optional[dict]:
        """Extract product data from embedded JSON."""
        # Try JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    return data
            except (json.JSONDecodeError, TypeError):
                pass

        # Try embedded product data
        for script in soup.find_all("script"):
            if script.string and "productData" in script.string:
                try:
                    match = re.search(
                        r"productData\s*=\s*(\{[^;]+\})", script.string, re.DOTALL
                    )
                    if match:
                        return json.loads(match.group(1))
                except (json.JSONDecodeError, AttributeError):
                    pass

        return None

    def _extract_title(self, soup: BeautifulSoup, json_data: Optional[dict]) -> str:
        """Extract product title."""
        if json_data and "name" in json_data:
            return json_data["name"]

        selectors = [
            "h1.product-title",
            "h1.diamond-title",
            ".product-name",
            "h1",
        ]
        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                text = self.extract_text(elem)
                if text:
                    return text
        return ""

    def _populate_from_json(self, item: GemstoneItem, data: dict) -> GemstoneItem:
        """Populate item from JSON data."""
        if "description" in data:
            item.description_text = data["description"]

        if "image" in data:
            imgs = data["image"]
            if isinstance(imgs, str):
                item.image_urls.append(imgs)
            elif isinstance(imgs, list):
                item.image_urls.extend(imgs)

        # Extract from structured properties
        props = data.get("additionalProperty", [])
        for prop in props:
            name = prop.get("name", "").lower()
            value = prop.get("value", "")

            if "shape" in name:
                item.shape = self.extract_shape(str(value))
            elif "carat" in name:
                item.carat = self.extract_carat(str(value))
            elif "lab" in name or "cert" in name:
                lab, report = self.extract_lab_info(str(value))
                if lab:
                    item.lab_name = lab
                    item.lab_report_id = report
                    item.has_lab_report = True

            item.extracted_attributes[name] = value

        return item

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract product images."""
        images = []
        seen = set()

        selectors = [
            ".product-image img",
            ".diamond-image img",
            ".gallery img",
            "[data-zoom-image]",
            "img[src*='diamond']",
        ]

        for selector in selectors:
            for elem in soup.select(selector):
                src = (
                    elem.get("data-zoom-image")
                    or elem.get("data-large")
                    or elem.get("data-src")
                    or elem.get("src")
                )
                if src:
                    full_url = urljoin(base_url, src)
                    if full_url not in seen and self._is_valid_image(full_url):
                        seen.add(full_url)
                        images.append(full_url)

        return images

    def _extract_specifications(self, soup: BeautifulSoup) -> dict:
        """Extract specifications."""
        specs = {}

        containers = soup.select(
            ".product-specs, .diamond-details, .specifications"
        )

        for container in containers:
            for row in container.select("tr, .spec-row"):
                cells = row.find_all(["th", "td", ".label", ".value"])
                if len(cells) >= 2:
                    key = self.extract_text(cells[0]).lower().strip(":")
                    value = self.extract_text(cells[1])
                    if key and value:
                        specs[key] = value

        return specs

    def _get_shape(self, specs: dict, title: str) -> Optional[str]:
        """Get shape from specs or title."""
        for key in ["shape", "cut"]:
            if key in specs:
                shape = self.extract_shape(specs[key])
                if shape:
                    return shape
        return self.extract_shape(title)

    def _get_carat(self, specs: dict, title: str) -> Optional[float]:
        """Get carat from specs or title."""
        for key in ["carat", "weight", "carat weight"]:
            if key in specs:
                carat = self.extract_carat(specs[key])
                if carat:
                    return carat
        return self.extract_carat(title)

    def _get_lab_info(
        self, specs: dict, title: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Get lab info from specs or title."""
        for key in ["lab", "certificate", "grading"]:
            if key in specs:
                lab, report = self.extract_lab_info(specs[key])
                if lab:
                    return lab, report

        for key in ["report", "certificate number"]:
            if key in specs:
                report = specs[key].strip()
                if re.match(r"^\d{10}$", report):
                    return "GIA", report

        return self.extract_lab_info(title)

    def _is_product_url(self, url: str) -> bool:
        """Check if URL is a product page."""
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
            "/engagement-rings", "/wedding", "/jewelry",
            "/education", "/about", "/help", ".pdf",
        ]
        return any(s in url_lower for s in skip)

    def _is_valid_image(self, url: str) -> bool:
        """Check if URL is a valid image."""
        url_lower = url.lower()
        skip = ["thumb", "icon", "logo", "banner", "placeholder"]
        if any(s in url_lower for s in skip):
            return False
        return any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
