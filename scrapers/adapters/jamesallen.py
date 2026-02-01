"""
JamesAllen.com scraper adapter.

Specializes in diamonds with 360 imagery.
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


@register_scraper("jamesallen.com")
class JamesAllenScraper(BaseScraper):
    """
    Scraper for jamesallen.com - diamonds with 360 imagery.

    Trusted fields:
    - lab_reports: true
    - shape: true
    - carat: true

    Ignored:
    - price
    - proprietary cut scores

    Notes:
    - Use 360 imagery when available
    """

    PRODUCT_URL_PATTERNS = [
        r"/loose-diamonds/",
        r"/diamond-details/",
        r"/diamond/",
        r"sku=",
    ]

    CATEGORY_URL_PATTERNS = [
        r"/diamonds/",
        r"/round-cut-diamonds",
        r"/princess-cut-diamonds",
        r"/cushion-cut-diamonds",
        r"/oval-cut-diamonds",
        r"/emerald-cut-diamonds",
        r"/pear-shaped-diamonds",
        r"/radiant-cut-diamonds",
        r"/asscher-cut-diamonds",
        r"/marquise-cut-diamonds",
        r"/heart-shaped-diamonds",
    ]

    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """Extract diamond item from James Allen product page."""
        soup = self.get_soup(html)

        # Try to extract data from JSON-LD first
        json_data = self._extract_json_ld(soup)

        # Extract title
        title = self._extract_title(soup, json_data)
        if not title:
            return None

        # Create base item
        item = self.create_base_item(url, title, compliance)
        item.stone_type_claimed = "diamond"

        # Extract from JSON data if available
        if json_data:
            item = self._populate_from_json(item, json_data)

        # Extract description
        desc_elem = soup.select_one(
            ".product-description, .diamond-description, [itemprop='description']"
        )
        if desc_elem:
            item.description_text = self.extract_text(desc_elem)

        # Extract images including 360 view frames
        item.image_urls = self._extract_images(soup, url)

        # Extract specifications
        specs = self._extract_specifications(soup)
        item.extracted_attributes.update(specs)

        # Override with spec values if not already set
        if not item.shape:
            item.shape = self._extract_shape_from_specs(specs, title)
        if not item.carat:
            item.carat = self._extract_carat_from_specs(specs, title)

        # Extract lab info
        if not item.lab_name:
            item.lab_name, item.lab_report_id = self._extract_lab_info(specs, title)
            item.has_lab_report = bool(item.lab_name)

        return item

    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """Discover diamond URLs from James Allen pages."""
        soup = self.get_soup(html)
        discovered = []
        seen = set()

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href:
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            # Check domain
            link_domain = parsed.netloc.lower()
            if link_domain.startswith("www."):
                link_domain = link_domain[4:]
            if link_domain != "jamesallen.com":
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
                        domain="jamesallen.com",
                        source_type="crawl",
                        depth=current_depth + 1,
                        priority=priority,
                    )
                )

        return discovered

    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """Check if URL is a product page."""
        return self._is_product_url(url)

    def _extract_json_ld(self, soup: BeautifulSoup) -> Optional[dict]:
        """Extract product data from JSON-LD."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    def _extract_title(self, soup: BeautifulSoup, json_data: Optional[dict]) -> str:
        """Extract product title."""
        if json_data and "name" in json_data:
            return json_data["name"]

        selectors = [
            "h1.product-title",
            "h1.diamond-title",
            ".product-name h1",
            "h1",
        ]
        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                text = self.extract_text(elem)
                if text:
                    return text
        return ""

    def _populate_from_json(self, item: GemstoneItem, json_data: dict) -> GemstoneItem:
        """Populate item fields from JSON-LD data."""
        if "description" in json_data:
            item.description_text = json_data["description"]

        if "image" in json_data:
            images = json_data["image"]
            if isinstance(images, str):
                item.image_urls.append(images)
            elif isinstance(images, list):
                item.image_urls.extend(images)

        # Extract from additional properties if present
        if "additionalProperty" in json_data:
            for prop in json_data["additionalProperty"]:
                name = prop.get("name", "").lower()
                value = prop.get("value", "")

                if "shape" in name or "cut" in name:
                    item.shape = self.extract_shape(str(value))
                elif "carat" in name or "weight" in name:
                    item.carat = self.extract_carat(str(value))
                elif "lab" in name or "cert" in name:
                    if not item.lab_name:
                        item.lab_name, item.lab_report_id = self.extract_lab_info(str(value))
                        item.has_lab_report = bool(item.lab_name)

                item.extracted_attributes[name] = value

        return item

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract images including 360 view frames."""
        images = []
        seen = set()

        # Standard image selectors
        selectors = [
            ".product-image img",
            ".diamond-image img",
            ".gallery img",
            "[data-zoom-image]",
            "img[src*='diamond']",
            "img[src*='stone']",
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

        # Look for 360 view data
        for script in soup.find_all("script"):
            if script.string and ("360" in script.string or "spin" in script.string.lower()):
                # Try to extract 360 image URLs
                matches = re.findall(
                    r'"(https?://[^"]+(?:360|spin)[^"]*\.(?:jpg|png|webp))"',
                    script.string,
                    re.I,
                )
                for match in matches:
                    if match not in seen:
                        seen.add(match)
                        images.append(match)

        return images

    def _extract_specifications(self, soup: BeautifulSoup) -> dict:
        """Extract specifications table."""
        specs = {}

        # Look for spec containers
        spec_containers = soup.select(
            ".product-specs, .diamond-specs, .specifications, .details"
        )

        for container in spec_containers:
            # Table format
            for row in container.select("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = self.extract_text(cells[0]).lower().strip(":")
                    value = self.extract_text(cells[1])
                    if key and value:
                        specs[key] = value

            # List format
            for item in container.select("li, .spec-item"):
                text = self.extract_text(item)
                if ":" in text:
                    key, value = text.split(":", 1)
                    specs[key.lower().strip()] = value.strip()

        return specs

    def _extract_shape_from_specs(self, specs: dict, title: str) -> Optional[str]:
        """Extract shape from specs or title."""
        shape_keys = ["shape", "cut", "shape/cut"]
        for key in shape_keys:
            if key in specs:
                shape = self.extract_shape(specs[key])
                if shape:
                    return shape
        return self.extract_shape(title)

    def _extract_carat_from_specs(self, specs: dict, title: str) -> Optional[float]:
        """Extract carat from specs or title."""
        carat_keys = ["carat", "weight", "carat weight"]
        for key in carat_keys:
            if key in specs:
                carat = self.extract_carat(specs[key])
                if carat:
                    return carat
        return self.extract_carat(title)

    def _extract_lab_info(
        self, specs: dict, title: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract lab certification info."""
        lab_keys = ["lab", "certificate", "certification", "grading"]
        for key in lab_keys:
            if key in specs:
                lab_name, report_id = self.extract_lab_info(specs[key])
                if lab_name:
                    return lab_name, report_id

        report_keys = ["report", "certificate number", "cert number"]
        for key in report_keys:
            if key in specs:
                report_id = specs[key].strip()
                if re.match(r"^\d{10}$", report_id):
                    return "GIA", report_id
                elif re.match(r"^\d{9}$", report_id):
                    return "IGI", report_id

        return self.extract_lab_info(title)

    def _is_product_url(self, url: str) -> bool:
        """Check if URL is a product page."""
        url_lower = url.lower()
        return any(re.search(pattern, url_lower) for pattern in self.PRODUCT_URL_PATTERNS)

    def _is_category_url(self, url: str) -> bool:
        """Check if URL is a category page."""
        url_lower = url.lower()
        return any(re.search(pattern, url_lower) for pattern in self.CATEGORY_URL_PATTERNS)

    def _is_skip_url(self, url: str) -> bool:
        """Check if URL should be skipped."""
        url_lower = url.lower()
        skip_patterns = [
            "/cart",
            "/checkout",
            "/account",
            "/login",
            "/register",
            "/wishlist",
            "/engagement-rings",
            "/wedding-rings",
            "/fine-jewelry",
            "/education",
            "/about",
            "/contact",
            ".pdf",
        ]
        return any(pattern in url_lower for pattern in skip_patterns)

    def _is_valid_image(self, url: str) -> bool:
        """Check if URL is a valid product image."""
        url_lower = url.lower()
        skip = ["thumb", "icon", "logo", "banner", "placeholder"]
        if any(s in url_lower for s in skip):
            return False
        return any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
