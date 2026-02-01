"""
Crawler module for URL discovery and fetching.

This module provides:
- robots.txt parsing and enforcement
- ToS compliance checking
- Rate limiting and politeness
- HTTP fetching with retries
"""

from crawler.robots import RobotsChecker, is_allowed_by_robots
from crawler.tos import ToSChecker, check_tos_compliance
from crawler.rate_limiter import RateLimiter, DomainRateLimiter
from crawler.fetcher import Fetcher, FetchResult

__all__ = [
    "RobotsChecker",
    "is_allowed_by_robots",
    "ToSChecker",
    "check_tos_compliance",
    "RateLimiter",
    "DomainRateLimiter",
    "Fetcher",
    "FetchResult",
]
