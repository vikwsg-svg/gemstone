"""Request and response models for /enterprise/v1 (these generate the OpenAPI spec)."""

import base64
import binascii
import datetime as dt
import hashlib
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise.engine.base import ItemQuery, MetalSpec, PhotoRef, StoneSpec
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
MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 8_000_000
PHOTO_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic")

ValuationBasis = Literal["retail_replacement", "fair_market", "wholesale", "auction_realized"]
ItemType = Literal["loose_stone", "ring", "earrings", "necklace", "pendant", "bracelet", "brooch", "other"]
StoneRole = Literal["center", "side", "accent"]
Treatment = Literal["none", "heated", "oiled", "diffused", "filled", "irradiated", "other"]
Growth = Literal["natural", "lab_grown"]
MetalType = Literal["gold", "platinum", "silver", "palladium", "other"]
Condition = Literal["new", "excellent", "very_good", "good", "fair", "poor"]
Lab = Literal["GIA", "IGI", "HRD", "AGL", "SSEF", "Gubelin", "GRS"]
SourceType = Literal["realized", "asking"]
ScanStatus = Literal["priced", "insufficient_data", "unidentified", "processing"]
PricingStatus = Literal["priced", "insufficient_data"]
ITEM_FIELDS = ("item_type", "stones", "metal", "brand", "signed", "era", "condition",
               "total_weight_grams")


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


class PhotoInput(BaseModel):
    """A photo of the item: a public https URL, or base64 image data with its media type."""

    model_config = ConfigDict(extra="forbid")

    url: Optional[str] = Field(default=None, max_length=2048, pattern=r"^https://",
                               description="Public https link to the photo.")
    data: Optional[str] = Field(default=None, description="Base64-encoded image, up to 8 MB.")
    media_type: Optional[Literal["image/jpeg", "image/png", "image/webp", "image/heic"]] = Field(
        default=None, description="Required with data."
    )

    @model_validator(mode="after")
    def _one_source(self) -> "PhotoInput":
        if (self.url is None) == (self.data is None):
            raise ValueError("a photo needs exactly one of url or data")
        if self.data is not None:
            if self.media_type is None:
                raise ValueError("media_type is required with data")
            try:
                raw = base64.b64decode(self.data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("data is not valid base64") from exc
            if not raw:
                raise ValueError("data is empty")
            if len(raw) > MAX_PHOTO_BYTES:
                raise ValueError(f"a photo may be at most {MAX_PHOTO_BYTES} bytes")
        return self

    def to_ref(self) -> PhotoRef:
        if self.data is not None:
            raw = base64.b64decode(self.data)
            return PhotoRef(sha256=hashlib.sha256(raw).hexdigest(), media_type=self.media_type,
                            bytes=len(raw), data=raw)
        return PhotoRef(sha256=hashlib.sha256(self.url.encode()).hexdigest(),
                        media_type=self.media_type, url=self.url)


class ItemFields(BaseModel):
    """What the item is. Required in full without photos; optional hints with photos."""

    model_config = ConfigDict(extra="forbid")

    item_type: Optional[ItemType] = Field(
        default=None, description="A loose stone or a kind of finished piece. Required without photos."
    )
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

    def stated(self) -> dict[str, Any]:
        """The item fields the caller actually provided, as plain JSON."""
        data = self.model_dump(mode="json", include=set(ITEM_FIELDS), exclude_none=True)
        if not data.get("stones"):
            data.pop("stones", None)
        return data

    def check_complete(self) -> None:
        """Rules for an item that can be priced. Raises ValueError."""
        if self.item_type is None:
            raise ValueError("item_type is required when no photos are sent")
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


class ScanRequest(ItemFields):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "photos": [{"url": "https://example.com/ring-front.jpg"},
                               {"url": "https://example.com/ring-inside-band.jpg"}],
                    "valuation_basis": "retail_replacement",
                },
                {
                    "photos": [{"url": "https://example.com/ring-front.jpg"}],
                    "item_type": "ring",
                    "metal": {"type": "platinum", "purity": "950", "weight_grams": 6.4},
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

    photos: list[PhotoInput] = Field(
        default_factory=list, max_length=MAX_PHOTOS,
        description="1-5 photos of the same item: front, side, and any stamps inside the band or "
                    "on the clasp. Optional when the item's details are given in full.",
    )
    description: Optional[str] = Field(default=None, max_length=2000,
                                       description="Free text, stored for audit; not used for pricing.")
    valuation_basis: ValuationBasis = Field(
        description="The value definition the price range is expressed in."
    )
    model_version: Optional[str] = Field(
        default=None, max_length=64,
        description="Pin a model version (overrides the account's pin). See GET /models.",
    )
    client_reference: Optional[str] = Field(default=None, max_length=128,
                                            description="Your own identifier, echoed back.")

    @model_validator(mode="after")
    def _photos_or_details(self) -> "ScanRequest":
        if not self.photos:
            self.check_complete()
        else:
            centers = [s for s in self.stones if s.role == "center"]
            if len(centers) > 1:
                raise ValueError("at most one stone may have role 'center'")
        return self

    def photo_refs(self) -> list[PhotoRef]:
        return [p.to_ref() for p in self.photos]

    def public_inputs(self) -> dict[str, Any]:
        """The request as stored and echoed: photo metadata instead of image data."""
        data = self.model_dump(mode="json", exclude={"photos"})
        data["photos"] = [ref.public() for ref in self.photo_refs()]
        return data


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


class Alternative(BaseModel):
    label: str
    probability: float = Field(ge=0, le=1)


class IdentificationOut(BaseModel):
    source: Literal["photos", "details", "photos_and_details"] = Field(
        description="What the identification is based on."
    )
    status: Literal["identified", "unidentified", "processing"]
    confidence: Optional[float] = Field(
        default=None, ge=0, le=1, description="Null when the item was described, not photographed."
    )
    summary: Optional[str] = Field(default=None, description="The preliminary identification.")
    observations: list[str] = Field(default_factory=list, description="What the photos show.")
    alternatives: list[Alternative] = Field(default_factory=list,
                                            description="Other things it could be, with probability.")
    open_questions: list[str] = Field(default_factory=list,
                                      description="What the photos cannot settle.")
    next_evidence: list[str] = Field(default_factory=list,
                                     description="The evidence or testing that would settle them.")
    field_sources: dict[str, Literal["stated", "identified"]] = Field(
        default_factory=dict,
        description="For each item field used, whether you stated it or it was identified.",
    )
    photos: list[dict[str, Any]] = Field(default_factory=list,
                                         description="Photo metadata (sha256, media type, size, url).")


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
    url: Optional[str] = Field(default=None, description="Link to the listing or sale record.")
    photo_url: Optional[str] = None
    differences: list[str] = Field(default_factory=list,
                                   description="How the comparable differs from this item.")
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
    status: PricingStatus
    price: Optional[Price]
    comps_used: int
    confidence: Confidence

    @model_validator(mode="after")
    def _floor(self) -> "ScenarioResult":
        _check_floor(self.status, self.price, self.comps_used)
        return self


def _check_floor(status: str, price: Optional[Price], comps_used: int) -> None:
    if status != "priced" and price is not None:
        raise ValueError(f"{status} must not carry a price")
    if status == "priced" and (price is None or comps_used < MIN_COMPS):
        raise ValueError(f"a price requires at least {MIN_COMPS} comps")


class ScanResponse(BaseModel):
    object: Literal["scan"] = "scan"
    scan_id: str
    created_at: dt.datetime
    status: ScanStatus = Field(
        description="priced | insufficient_data (fewer than 5 comps, no price) | unidentified "
                    "(the item could not be identified well enough to price) | processing."
    )
    product: Literal["scan"] = "scan"
    item_type: Optional[ItemType] = None
    model_version: str
    model_version_source: Literal["request", "tenant_pin", "default"]
    engine_version: str
    synthetic_data: bool = Field(
        description="True when computed in the sandbox from synthetic data (not market values)."
    )
    valuation_basis: ValuationBasis
    currency: Literal["USD"] = "USD"
    client_reference: Optional[str] = None
    inputs: dict[str, Any] = Field(description="The request as received, with photo metadata.")
    identification: IdentificationOut
    item: Optional[dict[str, Any]] = Field(default=None, description="The item that was priced.")
    price: Optional[Price] = Field(default=None, description="Null unless status is priced.")
    breakdown: Optional[Breakdown] = Field(
        default=None, description="Raw/Make/Market split of the mid value, for pieces."
    )
    confidence: Optional[Confidence] = Field(default=None, description="Null while processing.")
    comps: list[Comp] = Field(default_factory=list)
    comps_summary: Optional[CompsSummary] = None
    scenarios: list[ScenarioResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    batch_job_id: Optional[str] = None

    @model_validator(mode="after")
    def _floor(self) -> "ScanResponse":
        _check_floor(self.status, self.price, self.comps_summary.used if self.comps_summary else 0)
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
    scans_total: int
    scans_priced: int


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
