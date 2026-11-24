"""
The contracts between the enterprise layer and the GemstoneAI engines.

Two engines sit behind the Scan API:

- an **identify engine** (the scan's first step) turns 1-5 photos, plus any
  details the customer already knows, into a preliminary identification with
  alternatives, open questions and the next evidence that would settle it;
- a **comps engine** prices the identified item.

The comps engine contract:

An engine receives a normalized description of one item (a loose stone or a
finished piece of jewelry) and returns candidate comparables, each scored for
similarity (0-1) and tagged with its ``source_type``: ``realized`` (a
completed sale: auction hammer/realized price or a final resale price) or
``asking`` (a dealer or marketplace list price). For finished pieces it may
also return the Raw/Make/Market shares of the subject's value. The
enterprise layer then applies the rules that make the result
enterprise-grade: the 5-comp floor, valuation basis, confidence and
scenarios.

The production implementation is the V2 kNN engine, reached through
``enterprise.engine.remote.RemoteCompsEngine``.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Optional

SOURCE_TYPES = ("realized", "asking")
ITEM_TYPES = ("loose_stone", "ring", "earrings", "necklace", "pendant", "bracelet", "brooch", "other")
BREAKDOWN_PARTS = ("raw", "make", "market")


class EngineUnavailable(Exception):
    """The engine could not be reached or returned an unusable response."""


class IdentificationUnavailable(Exception):
    """The identify engine could not be reached or returned an unusable response."""


@dataclass(frozen=True)
class PhotoRef:
    """One photo of the item: inline bytes or a public https URL."""

    sha256: str
    media_type: Optional[str] = None
    bytes: Optional[int] = None
    url: Optional[str] = None
    data: Optional[bytes] = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        """What is stored and echoed back (never the image bytes)."""
        return {"sha256": self.sha256, "media_type": self.media_type,
                "bytes": self.bytes, "url": self.url}


@dataclass(frozen=True)
class Identification:
    """A preliminary identification, never a certainty."""

    status: str  # "identified" | "unidentified"
    item: Optional[Mapping[str, Any]] = None  # request-shaped item fields
    confidence: Optional[float] = None  # 0..1
    summary: Optional[str] = None
    observations: tuple[str, ...] = ()
    alternatives: tuple[Mapping[str, Any], ...] = ()  # {"label", "probability"}
    open_questions: tuple[str, ...] = ()
    next_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingIdentification:
    """The identify engine accepted the photos and is still working."""

    ref: str  # the engine's analysis id, polled later


class IdentifyEngine(ABC):
    name: str = "identify"
    synthetic: bool = False

    @abstractmethod
    def identify(self, photos: list[PhotoRef], hints: Mapping[str, Any]
                 ) -> "Identification | PendingIdentification":
        """Identify the item in ``photos``; ``hints`` are details the customer stated."""

    def poll(self, ref: str) -> "Identification | PendingIdentification":
        """Return the result of a pending identification (or still pending)."""
        raise IdentificationUnavailable("this identify engine does not support polling")


@dataclass(frozen=True)
class StoneSpec:
    variety: str
    carat: float  # per stone for count == 1; total weight of the group otherwise
    role: str = "center"  # center | side | accent
    count: int = 1
    species: Optional[str] = None
    shape: Optional[str] = None
    color_grade: Optional[str] = None
    clarity: Optional[str] = None
    cut_grade: Optional[str] = None
    treatment: Optional[str] = None  # None = unknown
    origin: Optional[str] = None  # None = unknown
    growth: Optional[str] = None  # natural | lab_grown | None = unknown
    lab: Optional[str] = None
    lab_report_number: Optional[str] = None
    length_mm: Optional[float] = None
    width_mm: Optional[float] = None
    depth_mm: Optional[float] = None


@dataclass(frozen=True)
class MetalSpec:
    type: str  # gold | platinum | silver | palladium | other
    purity: Optional[str] = None  # e.g. 18k, 14k, 950, 925
    color: Optional[str] = None  # yellow | white | rose | ...
    weight_grams: Optional[float] = None


@dataclass(frozen=True)
class ItemQuery:
    item_type: str
    stones: tuple[StoneSpec, ...] = ()
    metal: Optional[MetalSpec] = None
    brand: Optional[str] = None
    signed: Optional[bool] = None
    era: Optional[str] = None
    condition: Optional[str] = None
    total_weight_grams: Optional[float] = None

    @property
    def is_loose(self) -> bool:
        return self.item_type == "loose_stone"

    @property
    def principal(self) -> Optional[StoneSpec]:
        """The stone that drives value: the center stone, else the largest single stone."""
        if not self.stones:
            return None
        for s in self.stones:
            if s.role == "center":
                return s
        return max(self.stones, key=lambda s: s.carat / max(s.count, 1))

    def with_principal(self, **changes: Any) -> "ItemQuery":
        target = self.principal
        if target is None:
            return self
        stones = tuple(replace(s, **changes) if s is target else s for s in self.stones)
        return replace(self, stones=stones)

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
    similarity: float  # 0..1
    carat: Optional[float] = None  # loose stones: stone weight; pieces: optional
    attributes: Mapping[str, Any] = field(default_factory=dict)
    # Engine's adjustment of the comp toward the subject (e.g. V2 origin
    # multipliers). Loose stones use the per-carat form, pieces the total form.
    adjusted_price_per_carat: Optional[float] = None
    adjusted_price_total: Optional[float] = None
    # Evidence the customer can follow, as in the consumer scan's comparables.
    url: Optional[str] = None
    photo_url: Optional[str] = None
    differences: tuple[str, ...] = ()  # how the comp differs from the subject

    @property
    def price_per_carat(self) -> Optional[float]:
        if not self.carat:
            return None
        return self.price_total / self.carat

    @property
    def effective_price_per_carat(self) -> Optional[float]:
        if self.adjusted_price_per_carat is not None:
            return self.adjusted_price_per_carat
        return self.price_per_carat

    @property
    def effective_price_total(self) -> float:
        if self.adjusted_price_total is not None:
            return self.adjusted_price_total
        return self.price_total


@dataclass(frozen=True)
class EngineResult:
    comps: list[EngineComp]
    # Shares of the subject's value for finished pieces: {"raw", "make", "market"},
    # non-negative and summing to 1. None when the engine does not provide them.
    breakdown_shares: Optional[Mapping[str, float]] = None


class CompsEngine(ABC):
    #: Short identifier stored on every valuation.
    name: str = "engine"
    #: True when the engine serves synthetic (non-market) data.
    synthetic: bool = False

    def supports(self, engine_version: str) -> bool:
        return True

    @abstractmethod
    def find_comps(self, query: ItemQuery, *, engine_version: str, limit: int) -> EngineResult:
        """Return up to ``limit`` candidates, best first. May raise EngineUnavailable."""
