"""
The contract between the enterprise layer and a comps engine.

An engine receives a normalized stone description and returns candidate
comparables, each scored for similarity (0-1) and tagged with its
``source_type``: ``realized`` (a completed sale: auction hammer/realized
price or a final resale price) or ``asking`` (a dealer or marketplace list
price). The enterprise layer then applies the rules that make the result
enterprise-grade: the 5-comp floor, valuation basis, confidence and
scenarios.

The production implementation is the V2 kNN engine (species-specific
similarity weights, origin multipliers, 133+ varieties), reached through
``enterprise.engine.remote.RemoteCompsEngine``.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Optional

SOURCE_TYPES = ("realized", "asking")


class EngineUnavailable(Exception):
    """The engine could not be reached or returned an unusable response."""


@dataclass(frozen=True)
class StoneQuery:
    variety: str
    carat: float
    shape: str
    species: Optional[str] = None
    color_grade: Optional[str] = None
    clarity: Optional[str] = None
    treatment: Optional[str] = None  # None = unknown
    origin: Optional[str] = None  # None = unknown
    lab: Optional[str] = None
    lab_report_number: Optional[str] = None
    length_mm: Optional[float] = None
    width_mm: Optional[float] = None
    depth_mm: Optional[float] = None

    def with_overrides(self, **changes: Any) -> "StoneQuery":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EngineComp:
    comp_id: str
    source_type: str  # "realized" | "asking"
    source: str  # e.g. "auction:christies", "dealer:leibish"
    observed_on: date  # sale date (realized) or listing date (asking)
    currency: str
    price_total: float
    carat: float
    similarity: float  # 0..1
    attributes: Mapping[str, Any] = field(default_factory=dict)
    # Engine's price per carat after adjusting the comp toward the subject stone
    # (e.g. V2 origin multipliers). None means "use the raw price per carat".
    adjusted_price_per_carat: Optional[float] = None

    @property
    def price_per_carat(self) -> float:
        return self.price_total / self.carat

    @property
    def effective_price_per_carat(self) -> float:
        if self.adjusted_price_per_carat is not None:
            return self.adjusted_price_per_carat
        return self.price_per_carat


class CompsEngine(ABC):
    #: Short identifier stored on every valuation.
    name: str = "engine"
    #: True when the engine serves synthetic (non-market) data.
    synthetic: bool = False

    def supports(self, engine_version: str) -> bool:
        return True

    @abstractmethod
    def find_comps(
        self, query: StoneQuery, *, engine_version: str, limit: int
    ) -> list[EngineComp]:
        """Return up to ``limit`` candidates, best first. May raise EngineUnavailable."""
