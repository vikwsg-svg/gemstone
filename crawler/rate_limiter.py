"""
Rate limiting for polite crawling.

This module provides:
- Per-domain rate limiting
- Concurrency control
- Exponential backoff
"""

import asyncio
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import structlog

from config.allowed_sites import get_site_config
from config.settings import get_settings

logger = structlog.get_logger(__name__)


@dataclass
class DomainState:
    """State tracking for a domain."""

    domain: str
    last_request_time: float = 0.0
    request_count: int = 0
    active_requests: int = 0
    consecutive_errors: int = 0
    backoff_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """
    Simple rate limiter using token bucket algorithm.

    Limits requests to a maximum rate per second.
    """

    def __init__(self, rate: float = 1.0, burst: int = 1):
        """
        Initialize the rate limiter.

        Args:
            rate: Maximum requests per second
            burst: Maximum burst size
        """
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire a token, blocking if necessary.

        Args:
            timeout: Maximum time to wait (None for infinite)

        Returns:
            True if token acquired, False if timeout
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            with self._lock:
                now = time.monotonic()
                # Refill tokens based on time elapsed
                elapsed = now - self._last_update
                self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
                self._last_update = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                # Calculate wait time for next token
                wait_time = (1.0 - self._tokens) / self.rate

            # Check timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_time = min(wait_time, remaining)

            time.sleep(wait_time)

    def try_acquire(self) -> bool:
        """
        Try to acquire a token without blocking.

        Returns:
            True if token acquired
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last_update = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class DomainRateLimiter:
    """
    Per-domain rate limiter with concurrency control.

    Manages rate limits and concurrency for multiple domains,
    respecting site-specific configurations.
    """

    # Backoff settings
    BASE_BACKOFF = 2.0
    MAX_BACKOFF = 300.0  # 5 minutes
    BACKOFF_MULTIPLIER = 2.0

    def __init__(
        self,
        default_delay: Optional[float] = None,
        default_max_concurrent: Optional[int] = None,
        default_rate: Optional[float] = None,
    ):
        """
        Initialize the domain rate limiter.

        Args:
            default_delay: Default delay between requests (seconds)
            default_max_concurrent: Default max concurrent requests per domain
            default_rate: Default requests per second
        """
        settings = get_settings()
        self.default_delay = default_delay or settings.default_crawl_delay
        self.default_max_concurrent = (
            default_max_concurrent or settings.max_concurrent_per_domain
        )
        self.default_rate = default_rate or settings.max_requests_per_second

        self._domains: dict[str, DomainState] = {}
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._global_lock = threading.Lock()

    def _normalize_domain(self, domain: str) -> str:
        """Normalize domain string."""
        domain = domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def _get_domain_state(self, domain: str) -> DomainState:
        """Get or create domain state."""
        domain = self._normalize_domain(domain)

        with self._global_lock:
            if domain not in self._domains:
                self._domains[domain] = DomainState(domain=domain)
            return self._domains[domain]

    def _get_rate_limiter(self, domain: str) -> RateLimiter:
        """Get or create rate limiter for domain."""
        domain = self._normalize_domain(domain)

        with self._global_lock:
            if domain not in self._rate_limiters:
                # Check for site-specific config
                site_config = get_site_config(domain)
                if site_config:
                    rate = 1.0 / site_config.crawl_delay
                else:
                    rate = self.default_rate

                self._rate_limiters[domain] = RateLimiter(rate=rate, burst=1)

            return self._rate_limiters[domain]

    def _get_config(self, domain: str) -> tuple[float, int]:
        """Get delay and concurrency config for domain."""
        domain = self._normalize_domain(domain)
        site_config = get_site_config(domain)

        if site_config:
            return site_config.crawl_delay, site_config.max_concurrent
        return self.default_delay, self.default_max_concurrent

    def acquire(self, domain: str, timeout: Optional[float] = None) -> bool:
        """
        Acquire permission to make a request to a domain.

        This method:
        1. Checks concurrency limits
        2. Respects rate limits
        3. Handles backoff for errors

        Args:
            domain: The domain to request
            timeout: Maximum wait time

        Returns:
            True if permission acquired
        """
        domain = self._normalize_domain(domain)
        state = self._get_domain_state(domain)
        delay, max_concurrent = self._get_config(domain)
        rate_limiter = self._get_rate_limiter(domain)

        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            with state.lock:
                now = time.time()

                # Check backoff
                if now < state.backoff_until:
                    wait_time = state.backoff_until - now
                    logger.debug(
                        "domain_in_backoff",
                        domain=domain,
                        wait_time=wait_time,
                    )
                else:
                    # Check concurrency
                    if state.active_requests < max_concurrent:
                        # Check delay since last request
                        time_since_last = now - state.last_request_time
                        if time_since_last >= delay:
                            # Try rate limiter
                            if rate_limiter.try_acquire():
                                state.active_requests += 1
                                state.last_request_time = now
                                state.request_count += 1
                                return True

                        wait_time = delay - time_since_last
                    else:
                        # At concurrency limit
                        wait_time = 0.1

            # Check timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_time = min(wait_time, remaining)

            time.sleep(max(0.01, wait_time))

    def release(self, domain: str, success: bool = True):
        """
        Release a request slot for a domain.

        Args:
            domain: The domain
            success: Whether the request was successful
        """
        domain = self._normalize_domain(domain)
        state = self._get_domain_state(domain)

        with state.lock:
            state.active_requests = max(0, state.active_requests - 1)

            if success:
                state.consecutive_errors = 0
            else:
                state.consecutive_errors += 1
                # Apply exponential backoff
                backoff = min(
                    self.MAX_BACKOFF,
                    self.BASE_BACKOFF * (self.BACKOFF_MULTIPLIER ** state.consecutive_errors),
                )
                state.backoff_until = time.time() + backoff
                logger.warning(
                    "applied_backoff",
                    domain=domain,
                    consecutive_errors=state.consecutive_errors,
                    backoff_seconds=backoff,
                )

    def get_stats(self, domain: str) -> dict:
        """Get statistics for a domain."""
        domain = self._normalize_domain(domain)
        state = self._get_domain_state(domain)

        with state.lock:
            return {
                "domain": domain,
                "request_count": state.request_count,
                "active_requests": state.active_requests,
                "consecutive_errors": state.consecutive_errors,
                "in_backoff": time.time() < state.backoff_until,
            }

    def get_all_stats(self) -> dict[str, dict]:
        """Get statistics for all domains."""
        return {domain: self.get_stats(domain) for domain in self._domains}


# Global instance
_domain_rate_limiter: Optional[DomainRateLimiter] = None


def get_domain_rate_limiter() -> DomainRateLimiter:
    """Get the global domain rate limiter."""
    global _domain_rate_limiter
    if _domain_rate_limiter is None:
        _domain_rate_limiter = DomainRateLimiter()
    return _domain_rate_limiter
