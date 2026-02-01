"""
URL canonicalization for deduplication.

Normalizes URLs to detect duplicate content at different URLs.
"""

import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import structlog

logger = structlog.get_logger(__name__)


# URL parameters to always strip (tracking, session, etc.)
STRIP_PARAMS = {
    # Tracking
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    # Session
    "sessionid",
    "session_id",
    "sid",
    "jsessionid",
    "phpsessid",
    # Others
    "ref",
    "referrer",
    "source",
    "affiliate",
    "partner",
    "click",
    "track",
}

# Site-specific parameters to preserve
PRESERVE_PARAMS = {
    "leibish.com": {"sku", "id", "product"},
    "jamesallen.com": {"sku", "id", "diamond"},
    "bluenile.com": {"sku", "id"},
    "sothebys.com": {"lot", "id"},
    "christies.com": {"lot", "id"},
    "bonhams.com": {"lot", "id"},
}


def canonicalize_url(
    url: str,
    domain: Optional[str] = None,
) -> str:
    """
    Canonicalize a URL for deduplication.

    Normalizations:
    - Lowercase scheme and host
    - Remove www. prefix
    - Remove trailing slashes
    - Sort query parameters
    - Remove tracking parameters
    - Remove fragment

    Args:
        url: URL to canonicalize
        domain: Optional domain for site-specific rules

    Returns:
        Canonical URL string
    """
    try:
        parsed = urlparse(url)

        # Normalize scheme
        scheme = "https"  # Always use https

        # Normalize host
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]

        # Normalize path
        path = parsed.path
        # Remove trailing slash (but keep root /)
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        # Lowercase (most servers are case-insensitive)
        path = path.lower()
        # Normalize multiple slashes
        path = re.sub(r"/+", "/", path)

        # Get domain for site-specific rules
        if domain is None:
            domain = host

        # Process query parameters
        query_params = parse_qs(parsed.query, keep_blank_values=False)

        # Get site-specific preserved params
        preserved = PRESERVE_PARAMS.get(domain, set())

        # Filter parameters
        filtered_params = {}
        for key, values in query_params.items():
            key_lower = key.lower()
            # Keep if in preserved list or not in strip list
            if key_lower in preserved or (
                key_lower not in STRIP_PARAMS
                and not key_lower.startswith(("utm_", "_"))
            ):
                # Only keep first value for each param
                filtered_params[key_lower] = values[0]

        # Sort and encode query string
        if filtered_params:
            query = urlencode(sorted(filtered_params.items()))
        else:
            query = ""

        # Reconstruct URL (no fragment)
        canonical = urlunparse((scheme, host, path, "", query, ""))

        return canonical

    except Exception as e:
        logger.error("url_canonicalization_error", url=url, error=str(e))
        return url  # Return original on error


def extract_product_id(url: str) -> Optional[str]:
    """
    Try to extract a product/lot ID from a URL.

    Args:
        url: The URL to extract from

    Returns:
        Product ID if found, None otherwise
    """
    parsed = urlparse(url)

    # Check query parameters
    params = parse_qs(parsed.query)
    for param in ["sku", "id", "product_id", "lot", "item"]:
        if param in params:
            return params[param][0]

    # Check path patterns
    path = parsed.path

    # Pattern: /product/12345
    match = re.search(r"/(?:product|item|diamond|lot|sku)[/-]?(\d+)", path, re.I)
    if match:
        return match.group(1)

    # Pattern: /12345-product-name
    match = re.search(r"/(\d{4,})-", path)
    if match:
        return match.group(1)

    # Pattern: ending with /12345
    match = re.search(r"/(\d{4,})(?:\?|$)", url)
    if match:
        return match.group(1)

    return None


def is_duplicate_url(url1: str, url2: str) -> bool:
    """
    Check if two URLs point to the same content.

    Args:
        url1: First URL
        url2: Second URL

    Returns:
        True if URLs are duplicates
    """
    return canonicalize_url(url1) == canonicalize_url(url2)


class URLCanonicalizer:
    """
    URL canonicalizer with caching.

    Provides efficient canonicalization for large numbers of URLs.
    """

    def __init__(self):
        """Initialize the canonicalizer."""
        self._cache: dict[str, str] = {}
        self._product_ids: dict[str, str] = {}  # canonical -> product_id

    def canonicalize(self, url: str, domain: Optional[str] = None) -> str:
        """
        Canonicalize a URL with caching.

        Args:
            url: URL to canonicalize
            domain: Optional domain for site-specific rules

        Returns:
            Canonical URL
        """
        if url in self._cache:
            return self._cache[url]

        canonical = canonicalize_url(url, domain)
        self._cache[url] = canonical

        # Try to extract product ID
        product_id = extract_product_id(url)
        if product_id:
            self._product_ids[canonical] = product_id

        return canonical

    def is_duplicate(self, url1: str, url2: str) -> bool:
        """Check if two URLs are duplicates."""
        return self.canonicalize(url1) == self.canonicalize(url2)

    def get_product_id(self, url: str) -> Optional[str]:
        """
        Get product ID for a URL.

        Args:
            url: The URL

        Returns:
            Product ID if known
        """
        canonical = self.canonicalize(url)
        return self._product_ids.get(canonical)

    def find_duplicates(self, urls: list[str]) -> dict[str, list[str]]:
        """
        Find all duplicate groups in a list of URLs.

        Args:
            urls: List of URLs to check

        Returns:
            Dictionary of canonical_url -> list of original URLs
        """
        groups: dict[str, list[str]] = {}

        for url in urls:
            canonical = self.canonicalize(url)
            if canonical not in groups:
                groups[canonical] = []
            groups[canonical].append(url)

        # Only return groups with duplicates
        return {k: v for k, v in groups.items() if len(v) > 1}

    def clear_cache(self):
        """Clear the cache."""
        self._cache.clear()
        self._product_ids.clear()
