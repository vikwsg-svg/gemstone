"""
The contract between the enterprise layer and the GemstoneAI scan.

The enterprise Scan API is today's scan result with a business layer around
it, so the engine contract mirrors the scan's own three steps:

1. ``analyze``: 1-5 photos of one item (plus any details the customer
   already knows) -> a preliminary identification with observations,
   alternatives, open questions, the next evidence that would settle them,
   and price context when the identification supports one. May come back
   pending while the scan is still running.
2. ``get``: the result of a pending analysis.
3. ``comparables``: photographed market comparables for a completed
   analysis, each an asking price or a realized sale.

The enterprise layer adds what businesses need around that: keys, audit
storage, retries, usage, batch, a version stamp and the 5-comparable rule.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

PRICE_TYPES = ("asking", "realized")


class ScanUnavailable(Exception):
    """The scan service could not be reached or returned an unusable response."""


class UnsupportedModelVersion(Exception):
    def __init__(self, requested: str, available: list[str]):
        super().__init__(f"model version {requested!r} is not available")
        self.requested = requested
        self.available = available


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
class PriceContext:
    """The scan's price context: a range, or a statement of why there is none."""

    status: str  # "provided" | "withheld"
    currency: str = "USD"
    low: Optional[float] = None
    high: Optional[float] = None
    basis: Optional[str] = None  # what the figure represents, as the scan states it
    explanation: Optional[str] = None
    withheld_reason: Optional[str] = None


@dataclass(frozen=True)
class Comparable:
    source: str
    price: float
    currency: str
    price_type: str  # "asking" | "realized"
    observed_on: Optional[date] = None
    title: Optional[str] = None
    url: Optional[str] = None
    photo_url: Optional[str] = None
    differences: tuple[str, ...] = ()  # how it differs from the scanned item


@dataclass(frozen=True)
class AnalysisResult:
    """A completed scan analysis: preliminary, never a certainty."""

    analysis_id: str
    model_version: str
    identified: bool
    summary: Optional[str] = None
    confidence: Optional[float] = None
    observations: tuple[str, ...] = ()
    alternatives: tuple[Mapping[str, Any], ...] = ()  # {"label", "probability"}
    open_questions: tuple[str, ...] = ()
    next_evidence: tuple[str, ...] = ()
    price_context: PriceContext = PriceContext(status="withheld")


@dataclass(frozen=True)
class PendingAnalysis:
    analysis_id: str


class ScanEngine(ABC):
    #: Short identifier stored on every scan.
    name: str = "scan"
    #: True when the engine serves simulated or synthetic results.
    synthetic: bool = False

    @abstractmethod
    def analyze(self, photos: list[PhotoRef], details: Mapping[str, Any],
                model_version: Optional[str]) -> "AnalysisResult | PendingAnalysis":
        """Start a scan. May raise ScanUnavailable or UnsupportedModelVersion."""

    @abstractmethod
    def get(self, analysis_id: str) -> "AnalysisResult | PendingAnalysis":
        """The result of a pending analysis (or still pending)."""

    @abstractmethod
    def comparables(self, analysis_id: str) -> list[Comparable]:
        """Comparables for a completed analysis; an empty list means none were found."""
