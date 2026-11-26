"""Every scraped listing carries comp provenance: realized sale vs asking price."""

import pytest
from pydantic import ValidationError

from config.allowed_sites import ALLOWED_SITES, PRICE_SOURCE_TYPES, SiteConfig
from scrapers.models import ComplianceInfo, GemstoneItem
from storage.database import GemstoneItemModel

DEALERS = {"leibish.com", "jamesallen.com", "bluenile.com"}
AUCTION_HOUSES = {"sothebys.com", "christies.com", "bonhams.com"}


def test_every_allowed_site_has_a_price_source_type():
    assert set(ALLOWED_SITES) == DEALERS | AUCTION_HOUSES
    for domain, config in ALLOWED_SITES.items():
        assert config.price_source_type in PRICE_SOURCE_TYPES, domain


def test_dealers_default_to_asking_and_auction_houses_are_realized():
    assert {ALLOWED_SITES[d].price_source_type for d in DEALERS} == {"asking"}
    assert {ALLOWED_SITES[d].price_source_type for d in AUCTION_HOUSES} == {"realized"}
    # Default for any new site is the conservative "asking".
    assert SiteConfig(domain="new.example", purposes=(), expected_validation_tier=2).price_source_type == "asking"


def test_site_config_rejects_unknown_price_source_type():
    with pytest.raises(ValueError):
        SiteConfig(domain="x", purposes=(), expected_validation_tier=2, price_source_type="estimate")


def test_item_model_only_accepts_realized_or_asking():
    base = dict(source_site="leibish.com", source_url="https://leibish.com/x", title="t",
                stone_type_claimed="sapphire")
    assert GemstoneItem(**base).price_source_type is None
    assert GemstoneItem(**base, price_source_type="asking").price_source_type == "asking"
    with pytest.raises(ValidationError):
        GemstoneItem(**base, price_source_type="estimate")


def test_scrapers_stamp_items_from_site_config():
    from scrapers.adapters.leibish import LebishScraper
    from scrapers.adapters.sothebys import SothebysScraper

    compliance = ComplianceInfo(robots_allowed=True, tos_allowed=True, user_agent="test")
    dealer_item = LebishScraper("leibish.com").create_base_item("https://leibish.com/a", "t", compliance)
    auction_item = SothebysScraper("sothebys.com").create_base_item("https://sothebys.com/a", "t", compliance)
    assert dealer_item.price_source_type == "asking"
    assert auction_item.price_source_type == "realized"


def test_database_model_has_column():
    assert "price_source_type" in GemstoneItemModel.__table__.columns
