"""Shared helpers for enterprise tests."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from enterprise.engine.base import CompsEngine, EngineComp, StoneQuery

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
    """Returns a fixed list of comps (optionally per treatment) and records calls."""

    name = "fake"
    synthetic = False

    def __init__(self, comps=None, by_treatment=None, supported=None):
        self.comps = list(comps) if comps is not None else [make_comp(i) for i in range(12)]
        self.by_treatment = by_treatment or {}
        self.supported = supported
        self.calls: list[tuple[StoneQuery, str]] = []

    def supports(self, engine_version: str) -> bool:
        return self.supported is None or engine_version in self.supported

    def find_comps(self, query: StoneQuery, *, engine_version: str, limit: int):
        self.calls.append((query, engine_version))
        comps = self.by_treatment.get(query.treatment, self.comps)
        return list(comps)[:limit]


@dataclass
class TenantKey:
    tenant_id: str
    key: str
    key_id: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}


def stone(**overrides) -> dict:
    """A fully specified request (no scenarios triggered) unless overridden."""
    body = {
        "variety": "blue sapphire",
        "carat": 2.0,
        "shape": "oval",
        "treatment": "heated",
        "origin": "Sri Lanka",
        "lab": "GIA",
        "lab_report_number": "1234567890",
        "valuation_basis": "fair_market",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}
