"""
Scraper registry for managing site-specific adapters.

This module provides a central registry for all scrapers, ensuring
only scrapers for allowed domains can be registered and used.
"""

from typing import Optional, Type

from config.allowed_sites import get_site_config, is_domain_allowed
from scrapers.base import BaseScraper

# Registry of scraper classes
_SCRAPER_REGISTRY: dict[str, Type[BaseScraper]] = {}


def register_scraper(domain: str):
    """
    Decorator to register a scraper class for a domain.

    Usage:
        @register_scraper("leibish.com")
        class LeiboshScraper(BaseScraper):
            ...

    Args:
        domain: The domain this scraper handles

    Raises:
        ValueError: If domain is not in the allowlist
    """

    def decorator(cls: Type[BaseScraper]) -> Type[BaseScraper]:
        if not is_domain_allowed(domain):
            raise ValueError(
                f"Cannot register scraper for {domain}: domain not in allowlist"
            )
        _SCRAPER_REGISTRY[domain] = cls
        return cls

    return decorator


def get_scraper(domain: str) -> Optional[BaseScraper]:
    """
    Get a scraper instance for a domain.

    Args:
        domain: The domain to get a scraper for

    Returns:
        Scraper instance if available, None otherwise

    Raises:
        ValueError: If domain is not allowed
    """
    # Normalize domain
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]

    if not is_domain_allowed(domain):
        raise ValueError(f"Domain {domain} is not in the allowlist")

    scraper_class = _SCRAPER_REGISTRY.get(domain)
    if scraper_class:
        return scraper_class(domain)

    # Fall back to generic scraper if no specific one registered
    from scrapers.adapters.generic import GenericScraper

    return GenericScraper(domain)


def list_scrapers() -> dict[str, str]:
    """
    List all registered scrapers.

    Returns:
        Dict mapping domain to scraper class name
    """
    return {domain: cls.__name__ for domain, cls in _SCRAPER_REGISTRY.items()}


def has_specific_scraper(domain: str) -> bool:
    """
    Check if a domain has a specific scraper registered.

    Args:
        domain: The domain to check

    Returns:
        True if a specific scraper is registered
    """
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain in _SCRAPER_REGISTRY
