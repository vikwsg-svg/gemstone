"""
Robots.txt module (guardrails bypassed).

All methods return True/allow by default.
"""

from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class RobotsChecker:
    """Stub robots checker - always allows access."""

    def __init__(self, *args, **kwargs):
        """Initialize (no-op)."""
        pass

    def can_fetch(self, url: str) -> bool:
        """Always allow fetching."""
        return True

    def get_crawl_delay(self, url: str) -> Optional[float]:
        """No crawl delay."""
        return None

    def get_sitemaps(self, url: str) -> list[str]:
        """No sitemaps."""
        return []

    def clear_cache(self, domain: Optional[str] = None):
        """No-op."""
        pass

    def close(self):
        """No-op."""
        pass


_robots_checker: Optional[RobotsChecker] = None


def get_robots_checker() -> RobotsChecker:
    """Get the global robots checker instance."""
    global _robots_checker
    if _robots_checker is None:
        _robots_checker = RobotsChecker()
    return _robots_checker


def is_allowed_by_robots(url: str) -> bool:
    """Always allow - guardrails bypassed."""
    return True
