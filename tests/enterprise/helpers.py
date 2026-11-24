"""Shared helpers for enterprise tests."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from enterprise.engine.base import (
    CompsEngine,
    EngineComp,
    EngineResult,
    Identification,
    IdentificationUnavailable,
    IdentifyEngine,
    ItemQuery,
    PendingIdentification,
)

BASE = "/enterprise/v1"
FIXED_NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = FIXED_NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_comp(
    i: int,
    *,
    similarity: float = 0.9,
    source_type: str = "asking",
    ppc: float = 1000.0,
    carat: float = 2.0,
    origin: Optional[str] = "sri_lanka",
    observed_on: date = date(2026, 10, 1),
    currency: str = "USD",
    adjusted: Optional[float] = None,
) -> EngineComp:
    return EngineComp(
        comp_id=f"c{i:03d}",
        source_type=source_type,
        source="dealer:test" if source_type == "asking" else "auction:test",
        observed_on=observed_on,
        currency=currency,
        price_total=ppc * carat,
        carat=carat,
        similarity=similarity,
        attributes={"variety": "blue_sapphire", "origin": origin},
        adjusted_price_per_carat=adjusted,
    )


class FakeEngine(CompsEngine):
    """Returns a fixed list of comps (optionally per principal-stone treatment or
    growth) plus optional breakdown shares, and records calls."""

    name = "fake"
    synthetic = False

    def __init__(self, comps=None, by_treatment=None, by_growth=None, supported=None,
                 breakdown_shares=None):
        self.comps = list(comps) if comps is not None else [make_comp(i) for i in range(12)]
        self.by_treatment = by_treatment or {}
        self.by_growth = by_growth or {}
        self.supported = supported
        self.breakdown_shares = breakdown_shares
        self.calls: list[tuple[ItemQuery, str]] = []

    def supports(self, engine_version: str) -> bool:
        return self.supported is None or engine_version in self.supported

    def find_comps(self, query: ItemQuery, *, engine_version: str, limit: int) -> EngineResult:
        self.calls.append((query, engine_version))
        principal = query.principal
        comps = self.comps
        if principal is not None and principal.growth in self.by_growth:
            comps = self.by_growth[principal.growth]
        elif principal is not None and principal.treatment in self.by_treatment:
            comps = self.by_treatment[principal.treatment]
        return EngineResult(comps=list(comps)[:limit], breakdown_shares=self.breakdown_shares)


def make_piece_comp(i: int, *, price: float = 5000.0, similarity: float = 0.9,
                    source_type: str = "asking", adjusted: Optional[float] = None) -> EngineComp:
    return EngineComp(
        comp_id=f"p{i:03d}",
        source_type=source_type,
        source="dealer:test" if source_type == "asking" else "auction:test",
        observed_on=date(2026, 10, 1),
        currency="USD",
        price_total=price,
        similarity=similarity,
        carat=None,
        attributes={"item_type": "ring", "metal_type": "platinum"},
        adjusted_price_total=adjusted,
    )


@dataclass
class TenantKey:
    tenant_id: str
    key: str
    key_id: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}


STONE_KEYS = {
    "role", "variety", "species", "carat", "count", "shape", "color_grade", "clarity",
    "cut_grade", "treatment", "origin", "growth", "lab", "lab_report_number", "dimensions_mm",
}


def stone(**overrides) -> dict:
    """A fully specified loose-stone request (no scenarios triggered) unless overridden.

    Stone-level keys (variety, carat, treatment, ...) apply to the stone; the rest
    to the request. A value of None removes the key.
    """
    gem = {
        "variety": "blue sapphire",
        "carat": 2.0,
        "shape": "oval",
        "treatment": "heated",
        "origin": "Sri Lanka",
        "lab": "GIA",
        "lab_report_number": "1234567890",
    }
    body = {"item_type": "loose_stone", "valuation_basis": "fair_market"}
    for key, value in overrides.items():
        (gem if key in STONE_KEYS else body)[key] = value
    body = {k: v for k, v in body.items() if v is not None}
    body["stones"] = [{k: v for k, v in gem.items() if v is not None}]
    return body


def piece(**overrides) -> dict:
    """A fully specified ring with a natural diamond center unless overridden."""
    body = {
        "item_type": "ring",
        "metal": {"type": "platinum", "purity": "950", "weight_grams": 6.0},
        "stones": [{"role": "center", "variety": "diamond", "carat": 0.8, "shape": "round",
                    "color_grade": "G", "clarity": "VS2", "growth": "natural"}],
        "condition": "excellent",
        "valuation_basis": "retail_replacement",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


RING_IDENTIFICATION = Identification(
    status="identified",
    item={
        "item_type": "ring",
        "metal": {"type": "platinum", "purity": "950"},
        "stones": [
            {"role": "center", "variety": "diamond", "carat": 1.0, "shape": "round",
             "growth": "natural"},
            {"role": "side", "variety": "diamond", "carat": 0.4, "count": 12, "shape": "round"},
        ],
        "condition": "excellent",
    },
    confidence=0.9,
    summary="Platinum ring with a round diamond of about 1 ct",
    observations=("Round brilliant center stone", "PT950 stamp inside the band"),
    alternatives=({"label": "Center stone is moissanite", "probability": 0.05},),
    open_questions=("How much does the ring weigh?",),
    next_evidence=("Weigh the ring in grams",),
)


class FakeIdentifier(IdentifyEngine):
    """Returns a fixed identification; can stay pending for ``pending`` polls, or fail."""

    name = "fake-identify"
    synthetic = False

    def __init__(self, result: Identification = RING_IDENTIFICATION, pending: int = 0,
                 fail: bool = False):
        self.result = result
        self.pending = pending
        self.fail = fail
        self.calls: list[tuple[list, dict]] = []
        self.polls = 0

    def identify(self, photos, hints):
        self.calls.append((list(photos), dict(hints)))
        if self.fail:
            raise IdentificationUnavailable("down")
        if self.pending > 0:
            return PendingIdentification(ref="ana_test_123")
        return self.result

    def poll(self, ref):
        self.polls += 1
        if self.fail:
            raise IdentificationUnavailable("down")
        self.pending -= 1
        if self.pending > 0:
            return PendingIdentification(ref=ref)
        return self.result


def photo_url(n: int = 1) -> list[dict]:
    return [{"url": f"https://photos.example.com/item-{i}.jpg"} for i in range(n)]
