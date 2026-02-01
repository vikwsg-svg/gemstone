"""
Site-specific scraper adapters.

Each adapter handles parsing and URL discovery for a specific allowed domain.
"""

# Import all adapters to register them
from scrapers.adapters.generic import GenericScraper
from scrapers.adapters.leibish import LebishScraper
from scrapers.adapters.jamesallen import JamesAllenScraper
from scrapers.adapters.bluenile import BlueNileScraper
from scrapers.adapters.sothebys import SothebysScraper
from scrapers.adapters.christies import ChristiesScraper
from scrapers.adapters.bonhams import BonhamsScraper

__all__ = [
    "GenericScraper",
    "LebishScraper",
    "JamesAllenScraper",
    "BlueNileScraper",
    "SothebysScraper",
    "ChristiesScraper",
    "BonhamsScraper",
]
