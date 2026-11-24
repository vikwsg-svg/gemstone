"""Request and response models for /enterprise/v1 (these generate the OpenAPI spec)."""

import datetime as dt
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise.engine.base import ItemQuery, MetalSpec, StoneSpec
from enterprise.valuation.vocab import (
    normalize_free,
    normalize_growth,
    normalize_lab,
    normalize_metal_type,
    normalize_origin,
    normalize_purity,
    normalize_treatment,
    normalize_variety,
    species_for,
)

MIN_COMPS = 5
MAX_STONE_GROUPS = 50

ValuationBasis = Literal["retail_replacement", "fair_market", "wholesale", "auction_realized"]
ItemType = Literal["loose_stone", "ring", "earrings", "necklace", "pendant", "bracelet", "brooch", "other"]
StoneRole = Literal["center", "side", "accent"]
Treatment = Literal["none", "heated", "oiled", "diffused", "filled", "irradiated", "other"]
Growth = Literal["natural", "lab_grown"]
MetalType = Literal["gold", "platinum", "silver", "palladium", "other"]
Condition = Literal["new", "excellent", "very_good", "good", "fair", "poor"]
Lab = Literal["GIA", "IGI", "HRD", "AGL", "SSEF", "Gubelin", "GRS"]
SourceType = Literal["realized", "asking"]
ValuationStatus = Literal["priced", "insufficient_data"]


# ── Requests ─────────────────────────────────────────────────────────────────


class Dimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    length: float = Field(gt=0, le=200, description="Length in millimetres.")
    width: float = Field(gt=0, le=200, description="Width in millimetres.")
    depth: Optional[float] = Field(default=None, gt=0, le=200, description="Depth in millimetres.")


class StoneInput(BaseModel):
    """One stone, or a group of identical stones (e.g. 12 side diamonds, 0.40 ct total)."""

    model_config = ConfigDict(extra="forbid")

    role: StoneRole = Field(default="center", description="center, side or accent.")
    variety: str = Field(
        min_length=1, max_length=64,
        description="e.g. 'diamond', 'blue sapphire', 'ruby', 'emerald', 'spinel', 'pearl'.",
    )
    species: Optional[str] = Field(default=None, max_length=64,
                                   description="Inferred from variety when omitted.")
    carat: float = Field(gt=0, le=1000,
                         description="Weight of the stone, or total weight of the group if count > 1.")
    count: int = Field(default=1, ge=1, le=2000)
    shape: Optional[str] = Field(default=None, max_length=32,
                                 description="Cut shape, e.g. 'round', 'oval'. Required for a loose stone.")
    color_grade: Optional[str] = Field(default=None, max_length=64,
                                       description="e.g. 'G' for diamonds, 'vivid' for colored stones.")
    clarity: Optional[str] = Field(default=None, max_length=64, description="e.g. 'VS1', 'eye clean'.")
    cut_grade: Optional[str] = Field(default=None, max_length=32)
    treatment: Optional[Treatment] = Field(
        default=None,
        description="Omit when unknown; colored stones then get scenario pricing per treatment. "
                    "Synonyms accepted ('unheated', 'no heat', 'minor oil').",
    )
    origin: Optional[str] = Field(default=None, max_length=64, description="Omit when unknown.")
    growth: Optional[Growth] = Field(
        default=None,
        description="'natural' or 'lab_grown'. Omit when unknown; diamonds then get both scenarios.",
    )
    lab: Optional[Lab] = Field(default=None, description="Issuing lab. Requires lab_report_number.")
    lab_report_number: Optional[str] = Field(
        default=None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9\-/]+$"
    )
    dimensions_mm: Optional[Dimensions] = None

    @field_validator("treatment", mode="before")
    @classmethod
    def _treatment(cls, value: Any) -> Any:
        return normalize_treatment(value) if value is None or isinstance(value, str) else value

    @field_validator("growth", mode="before")
    @classmethod
    def _growth(cls, value: Any) -> Any:
        return normalize_growth(value) if value is None or isinstance(value, str) else value

    @field_validator("lab", mode="before")
    @classmethod
    def _lab(cls, value: Any) -> Any:
        return normalize_lab(value) if value is None or isinstance(value, str) else value

    @model_validator(mode="after")
    def _lab_pairing(self) -> "StoneInput":
        if (self.lab is None) != (self.lab_report_number is None):
            raise ValueError("lab and lab_report_number must be provided together")
        return self

    def to_spec(self) -> StoneSpec:
        variety = normalize_variety(self.variety)
        dims = self.dimensions_mm
        return StoneSpec(
            variety=variety,
            carat=self.carat,
            role=self.role,
            count=self.count,
            species=normalize_free(self.species) or species_for(variety),
            shape=normalize_free(self.shape),
            color_grade=normalize_free(self.color_grade),
            clarity=normalize_free(self.clarity),
            cut_grade=normalize_free(self.cut_grade),
            treatment=self.treatment,
            origin=normalize_origin(self.origin),
            growth=self.growth,
            lab=self.lab,
            lab_report_number=self.lab_report_number,
            length_mm=dims.length if dims else None,
            width_mm=dims.width if dims else None,
            depth_mm=dims.depth if dims else None,
        )


class MetalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MetalType = Field(description="gold, platinum, silver, palladium or other.")
    purity: Optional[str] = Field(default=None, max_length=16,
                                  description="e.g. '18k', '14k', '750', '950', '925'.")
    color: Optional[str] = Field(default=None, max_length=16, description="e.g. yellow, white, rose.")
    weight_grams: Optional[float] = Field(default=None, gt=0, le=5000)

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> Any:
        return normalize_metal_type(value) if isinstance(value, str) else value

    def to_spec(self) -> MetalSpec:
        return MetalSpec(
            type=self.type,
            purity=normalize_purity(self.purity),
            color=normalize_free(self.color),
            weight_grams=self.weight_grams,
        )


class ValuationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "item_type": "ring",
                    "metal": {"type": "platinum", "purity": "950", "weight_grams": 6.4},
                    "stones": [
                        {"role": "center", "variety": "blue sapphire", "carat": 2.1,
                         "shape": "oval", "origin": "Sri Lanka", "lab": "GIA",
                         "lab_report_number": "2235117890"},
                        {"role": "side", "variety": "diamond", "carat": 0.6, "count": 14,
                         "shape": "round", "growth": "natural"},
                    ],
                    "condition": "excellent",
                    "valuation_basis": "retail_replacement",
                },
                {
                    "item_type": "loose_stone",
                    "stones": [{"variety": "diamond", "carat": 1.52, "shape": "round",
                                "color_grade": "G", "clarity": "VS1", "growth": "natural",
                                "lab": "GIA", "lab_report_number": "6481234567"}],
                    "valuation_basis": "wholesale",
                },
            ]
        },
    )

    item_type: ItemType = Field(description="A loose stone or a kind of finished piece.")
    stones: list[StoneInput] = Field(
        default_factory=list, max_length=MAX_STONE_GROUPS,
        description="Exactly one stone for a loose stone; any number (including none) for a piece.",
    )
    metal: Optional[MetalInput] = Field(default=None, description="Not allowed for a loose stone.")
    brand: Optional[str] = Field(default=None, max_length=64,
                                 description="Maker or house, e.g. a jewelry maison.")
    signed: Optional[bool] = Field(default=None,
                                   description="True if the piece bears the maker's signature or mark.")
    era: Optional[str] = Field(default=None, max_length=32,
                               description="e.g. 'art deco', 'victorian', 'retro', 'contemporary'.")
    condition: Optional[Condition] = None
    total_weight_grams: Optional[float] = Field(default=None, gt=0, le=10000)
    description: Optional[str] = Field(default=None, max_length=2000,
                                       description="Free text, stored for audit; not used for pricing.")
    valuation_basis: ValuationBasis = Field(
        description="The value definition the price band is expressed in."
    )
    model_version: Optional[str] = Field(
        default=None, max_length=64,
        description="Pin a model version (overrides the account's pin). See GET /models.",
    )
    client_reference: Optional[str] = Field(default=None, max_length=128,
                                            description="Your own identifier, echoed back.")

    @model_validator(mode="after")
    def _consistency(self) -> "ValuationRequest":
        centers = [s for s in self.stones if s.role == "center"]
        if len(centers) > 1:
            raise ValueError("at most one stone may have role 'center'")
        if self.item_type == "loose_stone":
            if len(self.stones) != 1 or self.stones[0].count != 1:
                raise ValueError("a loose_stone needs exactly one stone with count 1")
            if not self.stones[0].shape:
                raise ValueError("a loose_stone needs a shape")
            if self.metal is not None:
                raise ValueError("a loose_stone cannot have metal")
        elif self.metal is None and not self.stones:
            raise ValueError("a piece needs metal, stones, or both")
        return self

    def to_query(self) -> ItemQuery:
        return ItemQuery(
            item_type=self.item_type,
            stones=tuple(s.to_spec() for s in self.stones),
            metal=self.metal.to_spec() if self.metal else None,
            brand=normalize_free(self.brand),
            signed=self.signed,
            era=normalize_free(self.era),
            condition=self.condition,
            total_weight_grams=self.total_weight_grams,
        )


# ── Responses ────────────────────────────────────────────────────────────────


class PriceBand(BaseModel):
    low: int
    mid: int
    high: int

    @model_validator(mode="after")
    def _ordered(self) -> "PriceBand":
        if not self.low <= self.mid <= self.high:
            raise ValueError("price band must satisfy low <= mid <= high")
        return self


class Price(BaseModel):
    per_carat: Optional[PriceBand] = Field(description="Loose stones only; null for pieces.")
    total: PriceBand


class BreakdownPart(BaseModel):
    share: float = Field(ge=0, le=1)
    value: int = Field(description="Share of the mid total, in the requested basis.")


class Breakdown(BaseModel):
    raw: BreakdownPart = Field(description="Materials: metal and stones.")
    make: BreakdownPart = Field(description="Craftsmanship and manufacture.")
    market: BreakdownPart = Field(description="Brand, period and market premium.")


class Reason(BaseModel):
    code: str = Field(description="Machine-readable reason code, e.g. 'few_comps'.")
    message: str


class Confidence(BaseModel):
    score: float = Field(ge=0, le=1)
    level: Literal["high", "medium", "low", "none"]
    reasons: list[Reason]


class Comp(BaseModel):
    comp_id: str
    source_type: SourceType = Field(
        description="'realized' = completed sale; 'asking' = list price, not a transaction."
    )
    source: str
    date: dt.date = Field(description="Sale date (realized) or listing date (asking).")
    currency: str
    carat: Optional[float] = None
    price_total: float
    price_per_carat: Optional[float] = None
    adjusted_price_per_carat: Optional[float] = Field(
        default=None, description="Engine adjustment toward the subject (loose stones)."
    )
    adjusted_price_total: Optional[float] = Field(
        default=None, description="Engine adjustment toward the subject (pieces)."
    )
    basis_factor: float = Field(description="Multiplier applied to reach the requested basis.")
    basis_price_per_carat: Optional[float] = None
    basis_price_total: float = Field(
        description="What this comp implies for the subject, in the requested basis."
    )
    similarity: float = Field(ge=0, le=1)
    qualified: bool = Field(description="True when the comp met the similarity threshold.")
    attributes: dict[str, Any]


class CompsSummary(BaseModel):
    considered: int
    rejected: int = Field(description="Comps dropped for missing source_type or invalid data.")
    qualified: int
    used: int
    realized: int
    asking: int
    min_required: int = MIN_COMPS
    similarity_threshold: float


class ScenarioResult(BaseModel):
    scenario_id: str
    label: str
    assumptions: dict[str, str]
    status: ValuationStatus
    price: Optional[Price]
    comps_used: int
    confidence: Confidence

    @model_validator(mode="after")
    def _floor(self) -> "ScenarioResult":
        _check_floor(self.status, self.price, self.comps_used)
        return self


def _check_floor(status: str, price: Optional[Price], comps_used: int) -> None:
    if status == "insufficient_data" and price is not None:
        raise ValueError("insufficient_data must not carry a price")
    if status == "priced" and (price is None or comps_used < MIN_COMPS):
        raise ValueError(f"a price requires at least {MIN_COMPS} comps")


class ValuationResponse(BaseModel):
    object: Literal["valuation"] = "valuation"
    valuation_id: str
    created_at: dt.datetime
    status: ValuationStatus
    product: Literal["comps"] = "comps"
    item_type: ItemType
    model_version: str
    model_version_source: Literal["request", "tenant_pin", "default"]
    engine_version: str
    synthetic_data: bool = Field(
        description="True when computed from sandbox synthetic items (not market values)."
    )
    valuation_basis: ValuationBasis
    currency: Literal["USD"] = "USD"
    client_reference: Optional[str] = None
    inputs: dict[str, Any] = Field(description="The request as received (after validation).")
    normalized_inputs: dict[str, Any] = Field(description="What the engine was asked to value.")
    price: Optional[Price] = Field(description="Null when status is insufficient_data.")
    breakdown: Optional[Breakdown] = Field(
        default=None, description="Raw/Make/Market split of the mid value, for pieces."
    )
    confidence: Confidence
    comps: list[Comp]
    comps_summary: CompsSummary
    scenarios: list[ScenarioResult]
    warnings: list[str]
    batch_job_id: Optional[str] = None

    @model_validator(mode="after")
    def _floor(self) -> "ValuationResponse":
        _check_floor(self.status, self.price, self.comps_summary.used)
        if self.breakdown is not None and self.price is None:
            raise ValueError("a breakdown requires a price")
        return self


class BatchJobResponse(BaseModel):
    object: Literal["batch_job"] = "batch_job"
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    created_at: dt.datetime
    started_at: Optional[dt.datetime] = None
    completed_at: Optional[dt.datetime] = None
    valuation_basis: Optional[ValuationBasis] = None
    model_version: Optional[str] = None
    total_rows: int
    processed_rows: int
    priced_rows: int
    insufficient_rows: int
    failed_rows: int
    results_url: Optional[str] = None
    error: Optional[str] = None


class UsageDay(BaseModel):
    date: dt.date
    product: str
    valuations_total: int
    valuations_priced: int


class UsageResponse(BaseModel):
    object: Literal["usage"] = "usage"
    tenant_id: str
    start_date: dt.date
    end_date: dt.date
    data: list[UsageDay]


class ModelVersionInfo(BaseModel):
    id: str
    engine_version: str
    released: dt.date
    status: Literal["current", "supported", "retired"]
    similarity_threshold: float
    notes: str


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    default_model_version: str
    tenant_pinned_model_version: Optional[str]
    data: list[ModelVersionInfo]


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
