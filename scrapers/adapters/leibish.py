"""
Leibish.com scraper adapter.

Specializes in fancy colored diamonds with GIA certification.
Expected validation tier: 0 (lab report verified)
"""

import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.models import ComplianceInfo, DiscoveredURL, GemstoneItem
from scrapers.registry import register_scraper


@register_scraper("leibish.com")
class LebishScraper(BaseScraper):
    """
    Scraper for leibish.com - fancy colored diamonds.

    Trusted fields:
    - lab_reports: true
    - stone_type: true
    - fancy_color_family: true
    - shape: true
    - carat: true

    Notes:
    - Prioritize listings that reference GIA reports
    - Loose stones only
    """

    # URL patterns for product pages
    PRODUCT_URL_PATTERNS = [
        r"/diamond/",
        r"/loose-diamond/",
        r"/fancy-color-diamond/",
        r"/natural-",
        r"-diamond-",
    ]

    # Patterns for listing pages to crawl
    LISTING_URL_PATTERNS = [
        r"/diamonds/",
        r"/fancy-colored-diamonds/",
        r"/yellow-diamonds/",
        r"/pink-diamonds/",
        r"/blue-diamonds/",
        r"/green-diamonds/",
    ]

    # Fancy color grades
    FANCY_COLORS = {
        "fancy light",
        "fancy",
        "fancy intense",
        "fancy vivid",
        "fancy deep",
        "fancy dark",
    }

    def extract_item(
        self,
        html: str,
        url: str,
        compliance: ComplianceInfo,
    ) -> Optional[GemstoneItem]:
        """Extract diamond item from Leibish product page."""
        soup = self.get_soup(html)

        # Extract title
        title_elem = soup.select_one("h1.product-name, h1.title, .product-title h1")
        if not title_elem:
            return None

        title = self.extract_text(title_elem)
        if not title:
            return None

        # Create base item
        item = self.create_base_item(url, title, compliance)

        # Default stone type for Leibish
        item.stone_type_claimed = "diamond"

        # Extract description
        desc_elem = soup.select_one(
            ".product-description, .description, [itemprop='description']"
        )
        item.description_text = self.extract_text(desc_elem) if desc_elem else ""

        # Extract images - Leibish often has high-quality product shots
        item.image_urls = self._extract_leibish_images(soup, url)

        # Extract specifications table
        specs = self._extract_specifications(soup)
        item.extracted_attributes = specs

        # Extract specific fields from specs
        item.shape = self._extract_shape_from_specs(specs, title)
        item.carat = self._extract_carat_from_specs(specs, title)
        item.color_family = self._extract_color_from_specs(specs, title)

        # Extract lab information
        item.lab_name, item.lab_report_id = self._extract_lab_from_specs(
            specs, f"{title} {item.description_text}"
        )
        item.has_lab_report = bool(item.lab_name)

        return item

    def discover_urls(
        self,
        html: str,
        base_url: str,
        current_depth: int,
    ) -> list[DiscoveredURL]:
        """Discover product URLs from Leibish pages."""
        soup = self.get_soup(html)
        discovered = []
        seen = set()

        # Find product links
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
            if link_domain != "leibish.com":
                continue

            # Canonicalize
            canonical = self.canonicalize_url(full_url)
            if canonical in seen:
                continue
            seen.add(canonical)

            # Skip non-product URLs
            if self._is_skip_url(full_url):
                continue

            # Determine priority
            priority = 0
            if self._is_product_url(full_url):
                priority = 10
            elif self._is_listing_url(full_url):
                priority = 5

            if priority > 0:
                discovered.append(
                    DiscoveredURL(
                        url=full_url,
                        domain="leibish.com",
                        source_type="crawl",
                        depth=current_depth + 1,
                        priority=priority,
                    )
                )

        return discovered

    def is_listing_page(self, url: str, html: Optional[str] = None) -> bool:
        """Check if URL is a Leibish product page."""
        return self._is_product_url(url)

    def _extract_leibish_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract high-quality images from Leibish page."""
        images = []
        seen = set()

        # Main product images
        selectors = [
            ".product-image img",
            ".gallery-image img",
            ".main-image img",
            "[data-zoom-image]",
            ".product-gallery img",
            "img[src*='leibish']",
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
                    # Prefer larger image versions
                    full_url = self._get_large_image_url(full_url)
                    if full_url not in seen and self._is_valid_product_image(full_url):
                        seen.add(full_url)
                        images.append(full_url)

        # Check for image data in JSON
        for script in soup.find_all("script"):
            if script.string and "productImages" in script.string:
                try:
                    # Try to extract image URLs from JS
                    matches = re.findall(r'"(https?://[^"]+\.(?:jpg|png|webp))"', script.string)
                    for match in matches:
                        if match not in seen and self._is_valid_product_image(match):
                            seen.add(match)
                            images.append(match)
                except Exception:
                    pass

        return images

    def _extract_specifications(self, soup: BeautifulSoup) -> dict:
        """Extract specifications from product page."""
        specs = {}

        # Look for spec table
        spec_selectors = [
            ".product-specs tr",
            ".specifications tr",
            ".product-attributes tr",
            ".details-table tr",
        ]

        for selector in spec_selectors:
            for row in soup.select(selector):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = self.extract_text(cells[0]).lower().strip(":")
                    value = self.extract_text(cells[1])
                    if key and value:
                        specs[key] = value

        # Also look for dl/dt/dd structures
        for dl in soup.select(".product-info dl, .specifications dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                key = self.extract_text(dt).lower().strip(":")
                value = self.extract_text(dd)
                if key and value:
                    specs[key] = value

        return specs

    def _extract_shape_from_specs(self, specs: dict, title: str) -> Optional[str]:
        """Extract shape from specs or title."""
        # Check specs
        shape_keys = ["shape", "cut", "shape/cut"]
        for key in shape_keys:
            if key in specs:
                shape = self.extract_shape(specs[key])
                if shape:
                    return shape

        # Check title
        return self.extract_shape(title)

    def _extract_carat_from_specs(self, specs: dict, title: str) -> Optional[float]:
        """Extract carat from specs or title."""
        # Check specs
        carat_keys = ["carat", "weight", "carat weight", "ct"]
        for key in carat_keys:
            if key in specs:
                carat = self.extract_carat(specs[key])
                if carat:
                    return carat

        # Check title
        return self.extract_carat(title)

    def _extract_color_from_specs(self, specs: dict, title: str) -> Optional[str]:
        """Extract color family from specs or title."""
        combined = f"{title} {' '.join(specs.values())}"

        # Look for fancy color grades first
        combined_lower = combined.lower()
        for fancy in self.FANCY_COLORS:
            if fancy in combined_lower:
                # Extract the color following the grade
                pattern = rf"{fancy}\s+(\w+)"
                match = re.search(pattern, combined_lower)
                if match:
                    return match.group(1)

        return self.extract_color_family(combined)

    def _extract_lab_from_specs(
        self, specs: dict, text: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract lab info from specs."""
        # Check specs for lab/certificate fields
        lab_keys = ["lab", "certificate", "certification", "grading lab", "report"]
        for key in lab_keys:
            if key in specs:
                lab_name, report_id = self.extract_lab_info(specs[key])
                if lab_name:
                    return lab_name, report_id

        # Check report number field
        report_keys = ["report number", "certificate number", "report #", "cert #"]
        for key in report_keys:
            if key in specs:
                # If we find a report number, assume GIA for Leibish
                report_id = specs[key].strip()
                if re.match(r"^\d{10}$", report_id):
                    return "GIA", report_id

        # Fall back to text search
        return self.extract_lab_info(text)

    def _is_product_url(self, url: str) -> bool:
        """Check if URL is a product page."""
        url_lower = url.lower()
        return any(re.search(pattern, url_lower) for pattern in self.PRODUCT_URL_PATTERNS)

    def _is_listing_url(self, url: str) -> bool:
        """Check if URL is a listing/category page."""
        url_lower = url.lower()
        return any(re.search(pattern, url_lower) for pattern in self.LISTING_URL_PATTERNS)

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
            "/compare",
            "/blog",
            "/about",
            "/contact",
            "/faq",
            "/help",
            "/education",
            "/guide",
            ".pdf",
            "javascript:",
            "#",
        ]
        return any(pattern in url_lower for pattern in skip_patterns)

    def _is_valid_product_image(self, url: str) -> bool:
        """Check if URL is a valid product image."""
        url_lower = url.lower()
        # Skip thumbnails, icons, etc.
        skip_patterns = ["thumb", "icon", "logo", "banner", "placeholder", "tiny"]
        if any(pattern in url_lower for pattern in skip_patterns):
            return False
        # Must be an image
        image_exts = [".jpg", ".jpeg", ".png", ".webp"]
        return any(ext in url_lower for ext in image_exts)

    def _get_large_image_url(self, url: str) -> str:
        """Try to get the largest version of an image URL."""
        # Common patterns for image size parameters
        url = re.sub(r"_\d+x\d+\.", ".", url)
        url = re.sub(r"\?.*$", "", url)  # Remove query params that might limit size
        return url
