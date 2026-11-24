"""
Model versions and pinning.

A *model version* fixes everything that can move a number: the engine
version (comp retrieval and similarity) plus the enterprise rules on top
(similarity threshold, comp cap, source weights, basis factors, band
quantiles). Stamping it on every valuation and letting a customer pin it is
what makes a valuation reproducible across a sale or a policy term.

Resolution order: request ``model_version`` > tenant pin > registry default.

NOTE: the basis factors and engine version labels below are placeholders
pending sign-off by the pricing team; see docs/enterprise/README.md.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

VALUATION_BASES = ("retail_replacement", "fair_market", "wholesale", "auction_realized")

# Multiplier from a comp's price (by source type) to the requested basis.
# PLACEHOLDERS: to be calibrated by the pricing team on realized-sale data.
DEFAULT_BASIS_FACTORS: Mapping[str, Mapping[str, float]] = {
    "asking": {
        "retail_replacement": 1.00,
        "fair_market": 0.80,
        "wholesale": 0.55,
        "auction_realized": 0.70,
    },
    "realized": {
        "retail_replacement": 1.40,
        "fair_market": 1.00,
        "wholesale": 0.75,
        "auction_realized": 1.00,
    },
}


@dataclass(frozen=True)
class ModelVersion:
    id: str
    engine_version: str
    released: date
    status: str  # "current" | "supported" | "retired"
    similarity_threshold: float = 0.55
    max_comps: int = 25
    candidate_limit: int = 60
    # Realized sales count for more than asking prices when building the band.
    source_weights: Mapping[str, float] = field(
        default_factory=lambda: {"realized": 1.0, "asking": 0.7}
    )
    basis_factors: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: DEFAULT_BASIS_FACTORS
    )
    band_quantiles: tuple[float, float, float] = (0.25, 0.50, 0.75)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("current", "supported", "retired"):
            raise ValueError(f"invalid status {self.status!r}")
        for source in ("realized", "asking"):
            missing = set(VALUATION_BASES) - set(self.basis_factors.get(source, {}))
            if missing:
                raise ValueError(f"{self.id}: missing basis factors for {source}: {missing}")
        low, mid, high = self.band_quantiles
        if not 0 < low < mid < high < 1:
            raise ValueError(f"{self.id}: band quantiles must be increasing in (0, 1)")

    def basis_factor(self, source_type: str, basis: str) -> float:
        return float(self.basis_factors[source_type][basis])

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "engine_version": self.engine_version,
            "released": self.released.isoformat(),
            "status": self.status,
            "similarity_threshold": self.similarity_threshold,
            "max_comps": self.max_comps,
            "source_weights": dict(self.source_weights),
            "basis_factors": {k: dict(v) for k, v in self.basis_factors.items()},
            "band_quantiles": list(self.band_quantiles),
            "notes": self.notes,
        }


DEFAULT_VERSIONS: tuple[ModelVersion, ...] = (
    ModelVersion(
        id="scan-2026.11.0",
        engine_version="v2.4",
        released=date(2026, 11, 24),
        status="current",
        notes="Enterprise v1 launch rules on engine v2.4.",
    ),
    ModelVersion(
        id="scan-2026.09.0",
        engine_version="v2.3",
        released=date(2026, 9, 1),
        status="supported",
        similarity_threshold=0.50,
        source_weights={"realized": 1.0, "asking": 0.8},
        notes="Pre-launch rules on engine v2.3; kept for customers who pinned it.",
    ),
)


class UnknownModelVersion(Exception):
    def __init__(self, requested: str, source: str, available: list[str]):
        super().__init__(f"unknown or retired model version {requested!r} (from {source})")
        self.requested = requested
        self.source = source
        self.available = available


class ModelRegistry:
    def __init__(
        self, versions: Iterable[ModelVersion] = DEFAULT_VERSIONS, default_id: Optional[str] = None
    ):
        self._versions = {v.id: v for v in versions}
        if default_id is not None:
            if default_id not in self._versions or self._versions[default_id].status == "retired":
                raise ValueError(f"default model version {default_id!r} is not available")
            self._default = self._versions[default_id]
        else:
            current = [v for v in self._versions.values() if v.status == "current"]
            if len(current) != 1:
                raise ValueError("exactly one model version must have status 'current'")
            self._default = current[0]

    @property
    def default(self) -> ModelVersion:
        return self._default

    def get(self, version_id: str) -> Optional[ModelVersion]:
        return self._versions.get(version_id)

    def available(self) -> list[ModelVersion]:
        return sorted(
            (v for v in self._versions.values() if v.status != "retired"),
            key=lambda v: v.released,
            reverse=True,
        )

    def resolve(
        self, requested: Optional[str], tenant_pin: Optional[str]
    ) -> tuple[ModelVersion, str]:
        """Return (model version, source) where source is request | tenant_pin | default."""
        for candidate, source in ((requested, "request"), (tenant_pin, "tenant_pin")):
            if candidate:
                version = self._versions.get(candidate)
                if version is None or version.status == "retired":
                    raise UnknownModelVersion(
                        candidate, source, [v.id for v in self.available()]
                    )
                return version, source
        return self._default, "default"
