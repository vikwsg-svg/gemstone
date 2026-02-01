"""
HTTP fetcher with retries and proper error handling.

This module provides:
- Reliable HTTP fetching
- Automatic retries with exponential backoff
- Proper error handling and logging
- Integration with rate limiter
"""

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.allowed_sites import is_domain_allowed
from config.settings import get_settings
from crawler.rate_limiter import DomainRateLimiter, get_domain_rate_limiter
from crawler.robots import RobotsChecker, get_robots_checker

logger = structlog.get_logger(__name__)


class FetchStatus(Enum):
    """Status of a fetch operation."""

    SUCCESS = "success"
    ROBOTS_DENIED = "robots_denied"
    DOMAIN_NOT_ALLOWED = "domain_not_allowed"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    RATE_LIMITED = "rate_limited"


@dataclass
class FetchResult:
    """Result of a fetch operation."""

    url: str
    status: FetchStatus
    http_status_code: Optional[int] = None
    content: Optional[str] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    elapsed_ms: float = 0.0
    error_message: Optional[str] = None
    cached: bool = False
    robots_allowed: bool = True
    redirected_url: Optional[str] = None

    @property
    def success(self) -> bool:
        """Check if fetch was successful."""
        return self.status == FetchStatus.SUCCESS


class Fetcher:
    """
    HTTP fetcher with rate limiting, retries, and compliance checks.

    This class handles all HTTP fetching with:
    - robots.txt compliance
    - Domain allowlist enforcement
    - Rate limiting
    - Automatic retries
    - Proper error handling
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        rate_limiter: Optional[DomainRateLimiter] = None,
        robots_checker: Optional[RobotsChecker] = None,
        respect_robots: bool = True,
        storage_path: Optional[str] = None,
    ):
        """
        Initialize the fetcher.

        Args:
            user_agent: User agent string
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
            rate_limiter: Rate limiter instance
            robots_checker: Robots checker instance
            respect_robots: Whether to respect robots.txt
            storage_path: Path to store raw HTML snapshots
        """
        settings = get_settings()

        self.user_agent = user_agent or settings.default_user_agent
        self.timeout = timeout or settings.request_timeout
        self.max_retries = max_retries or settings.max_retries
        self.respect_robots = respect_robots and settings.respect_robots_txt

        self.rate_limiter = rate_limiter or get_domain_rate_limiter()
        self.robots_checker = robots_checker or get_robots_checker()

        self.storage_path = Path(storage_path or settings.raw_html_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self._client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                },
            )
        return self._client

    def _get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def _check_compliance(self, url: str) -> tuple[bool, Optional[FetchStatus]]:
        """
        Check compliance before fetching.

        Returns:
            Tuple of (allowed, failure_status)
        """
        domain = self._get_domain(url)

        # Check allowlist
        if not is_domain_allowed(domain):
            logger.warning("domain_not_allowed", url=url, domain=domain)
            return False, FetchStatus.DOMAIN_NOT_ALLOWED

        # Check robots.txt
        if self.respect_robots and not self.robots_checker.can_fetch(url):
            logger.warning("robots_denied", url=url)
            return False, FetchStatus.ROBOTS_DENIED

        return True, None

    def _save_snapshot(self, url: str, content: str) -> str:
        """
        Save HTML snapshot to storage.

        Args:
            url: The source URL
            content: HTML content

        Returns:
            Path to saved snapshot
        """
        domain = self._get_domain(url)
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{domain}_{timestamp}_{url_hash}.html"
        filepath = self.storage_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath)

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _do_fetch(self, url: str) -> httpx.Response:
        """Perform the actual HTTP request with retry logic."""
        client = self._get_client()
        return client.get(url)

    def fetch(
        self,
        url: str,
        save_snapshot: bool = True,
        rate_limit_timeout: float = 60.0,
    ) -> FetchResult:
        """
        Fetch a URL with full compliance and rate limiting.

        Args:
            url: The URL to fetch
            save_snapshot: Whether to save HTML snapshot
            rate_limit_timeout: Max time to wait for rate limit

        Returns:
            FetchResult with status and content
        """
        domain = self._get_domain(url)
        start_time = time.time()

        # Check compliance
        allowed, failure_status = self._check_compliance(url)
        if not allowed:
            return FetchResult(
                url=url,
                status=failure_status,
                robots_allowed=(failure_status != FetchStatus.ROBOTS_DENIED),
            )

        # Acquire rate limit
        if not self.rate_limiter.acquire(domain, timeout=rate_limit_timeout):
            logger.warning("rate_limit_timeout", url=url)
            return FetchResult(
                url=url,
                status=FetchStatus.RATE_LIMITED,
                error_message="Rate limit timeout",
            )

        success = False
        try:
            response = self._do_fetch(url)
            success = True

            elapsed_ms = (time.time() - start_time) * 1000
            content = response.text
            content_type = response.headers.get("content-type", "")

            # Check for successful response
            if response.status_code != 200:
                logger.warning(
                    "http_error",
                    url=url,
                    status_code=response.status_code,
                )
                return FetchResult(
                    url=url,
                    status=FetchStatus.HTTP_ERROR,
                    http_status_code=response.status_code,
                    elapsed_ms=elapsed_ms,
                    error_message=f"HTTP {response.status_code}",
                )

            # Save snapshot if requested
            snapshot_path = None
            if save_snapshot and content:
                snapshot_path = self._save_snapshot(url, content)

            # Check for redirects
            redirected_url = None
            if str(response.url) != url:
                redirected_url = str(response.url)

            logger.info(
                "fetch_success",
                url=url,
                status_code=response.status_code,
                content_length=len(content),
                elapsed_ms=elapsed_ms,
            )

            return FetchResult(
                url=url,
                status=FetchStatus.SUCCESS,
                http_status_code=response.status_code,
                content=content,
                content_type=content_type,
                content_length=len(content),
                elapsed_ms=elapsed_ms,
                redirected_url=redirected_url,
            )

        except httpx.TimeoutException as e:
            logger.error("fetch_timeout", url=url, error=str(e))
            return FetchResult(
                url=url,
                status=FetchStatus.TIMEOUT,
                elapsed_ms=(time.time() - start_time) * 1000,
                error_message=str(e),
            )

        except httpx.NetworkError as e:
            logger.error("fetch_network_error", url=url, error=str(e))
            return FetchResult(
                url=url,
                status=FetchStatus.NETWORK_ERROR,
                elapsed_ms=(time.time() - start_time) * 1000,
                error_message=str(e),
            )

        except Exception as e:
            logger.error("fetch_error", url=url, error=str(e))
            return FetchResult(
                url=url,
                status=FetchStatus.HTTP_ERROR,
                elapsed_ms=(time.time() - start_time) * 1000,
                error_message=str(e),
            )

        finally:
            self.rate_limiter.release(domain, success=success)

    def fetch_image(
        self,
        url: str,
        save_path: Optional[str] = None,
    ) -> tuple[bool, Optional[bytes], Optional[str]]:
        """
        Fetch an image.

        Args:
            url: Image URL
            save_path: Optional path to save image

        Returns:
            Tuple of (success, content_bytes, error_message)
        """
        domain = self._get_domain(url)

        # Check compliance
        allowed, _ = self._check_compliance(url)
        if not allowed:
            return False, None, "Domain not allowed"

        # Acquire rate limit
        if not self.rate_limiter.acquire(domain, timeout=30.0):
            return False, None, "Rate limit timeout"

        success = False
        try:
            client = self._get_client()
            response = client.get(url)
            success = True

            if response.status_code != 200:
                return False, None, f"HTTP {response.status_code}"

            content = response.content

            if save_path:
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(content)

            return True, content, None

        except Exception as e:
            return False, None, str(e)

        finally:
            self.rate_limiter.release(domain, success=success)

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None


# Global instance
_fetcher: Optional[Fetcher] = None


def get_fetcher() -> Fetcher:
    """Get the global fetcher instance."""
    global _fetcher
    if _fetcher is None:
        _fetcher = Fetcher()
    return _fetcher
