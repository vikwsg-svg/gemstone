"""Request and response models for /enterprise/v1 (these generate the OpenAPI spec)."""

import datetime as dt
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise.engine.base import StoneQuery
from enterprise.valuation.vocab import (
    normalize_free,
    normalize_lab,
    normalize_origin,
    normalize_treatment,
    normalize_variety,
    species_for,
)

MIN_COMPS = 5

ValuationBasis = Literal["retail_replacement", "fair_market", "wholesale", "auction_realized"]
Treatment = Literal["none", "heated", "oiled", "diffused", "filled", "irradiated", "other"]
Lab = Literal["GIA", "AGL", "SSEF", "Gubelin", "GRS"]
SourceType = Literal["realized", "asking"]
ValuationStatus = Literal["priced", "insufficient_data"]


# ── Requests ─────────────────────────────────────────────────────────────────


class Dimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    length: float = Field(gt=0, le=200, description="Length in millimetres.")
    width: float = Field(gt=0, le=200, description="Width in millimetres.")
    depth: Optional[float] = Field(default=None, gt=0, le=200, description="Depth in millimetres.")


class ValuationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "variety": "blue sapphire",
                    "carat": 2.1,
                    "shape": "oval",
                    "color_grade": "intense",
                    "clarity": "eye clean",
                    "origin": "Sri Lanka",
                    "lab": "GIA",
                    "lab_report_number": "2235117890",
                    "valuation_basis": "retail_replacement",
                }
            ]
        },
    )

    variety: str = Field(
        min_length=1,
        max_length=64,
        description="Gem variety (e.g. 'blue sapphire', 'ruby', 'emerald', 'spinel'). Required.",
    )
    species: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Mineral species (e.g. 'corundum'). Inferred from variety when omitted.",
    )
    carat: float = Field(gt=0, le=1000, description="Weight in carats. Required.")
    shape: str = Field(min_length=1, max_length=32, description="Cut shape (e.g. 'oval'). Required.")
    color_grade: Optional[str] = Field(default=None, max_length=64)
    clarity: Optional[str] = Field(default=None, max_length=64)
    treatment: Optional[Treatment] = Field(
        default=None,
        description=(
            "Omit or send null when unknown; the response then includes scenario pricing "
            "per plausible treatment. Common synonyms ('unheated', 'no heat', 'minor oil') "
            "are accepted."
        ),
    )
    origin: Optional[str] = Field(
        default=None, max_length=64, description="Geographic origin; omit when unknown."
    )
    lab: Optional[Lab] = Field(default=None, description="Issuing lab. Requires lab_report_number.")
    lab_report_number: Optional[str] = Field(
        default=None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9\-/]+$"
    )
    dimensions_mm: Optional[Dimensions] = None
    valuation_basis: ValuationBasis = Field(
        description="The value definition the price band is expressed in."
    )
    model_version: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Pin a model version (overrides the account's pin). See GET /models.",
    )
    client_reference: Optional[str] = Field(
        default=None, max_length=128, description="Your own identifier, echoed back."
    )

    @field_validator("treatment", mode="before")
    @classmethod
    def _normalize_treatment(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return normalize_treatment(value)
        return value

    @field_validator("lab", mode="before")
    @classmethod
    def _normalize_lab(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return normalize_lab(value)
        return value

    @model_validator(mode="after")
    def _lab_pairing(self) -> "ValuationRequest":
        if (self.lab is None) != (self.lab_report_number is None):
            raise ValueError("lab and lab_report_number must be provided together")
        return self

    def to_query(self) -> StoneQuery:
        variety = normalize_variety(self.variety)
        dims = self.dimensions_mm
        return StoneQuery(
            variety=variety,
            species=normalize_free(self.species) or species_for(variety),
            carat=self.carat,
            shape=normalize_free(self.shape) or "",
            color_grade=normalize_free(self.color_grade),
            clarity=normalize_free(self.clarity),
            treatment=self.treatment,
            origin=normalize_origin(self.origin),
            lab=self.lab,
            lab_report_number=self.lab_report_number,
            length_mm=dims.length if dims else None,
            width_mm=dims.width if dims else None,
            depth_mm=dims.depth if dims else None,
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
    per_carat: PriceBand
    total: PriceBand


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
    carat: float
    price_total: float
    price_per_carat: float
    adjusted_price_per_carat: Optional[float] = Field(
        default=None, description="Engine-adjusted price per carat toward the subject stone."
    )
    basis_factor: float = Field(description="Multiplier applied to reach the requested basis.")
    basis_price_per_carat: float
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
    model_version: str
    model_version_source: Literal["request", "tenant_pin", "default"]
    engine_version: str
    synthetic_data: bool = Field(
        description="True when computed from sandbox synthetic stones (not market values)."
    )
    valuation_basis: ValuationBasis
    currency: Literal["USD"] = "USD"
    client_reference: Optional[str] = None
    inputs: dict[str, Any] = Field(description="The request as received (after validation).")
    normalized_inputs: dict[str, Any] = Field(description="What the engine was asked to value.")
    price: Optional[Price] = Field(description="Null when status is insufficient_data.")
    confidence: Confidence
    comps: list[Comp]
    comps_summary: CompsSummary
    scenarios: list[ScenarioResult]
    warnings: list[str]
    batch_job_id: Optional[str] = None

    @model_validator(mode="after")
    def _floor(self) -> "ValuationResponse":
        _check_floor(self.status, self.price, self.comps_summary.used)
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
