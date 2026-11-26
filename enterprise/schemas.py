"""Request and response models for /enterprise/v1 (these generate the OpenAPI spec)."""

import base64
import binascii
import datetime as dt
import hashlib
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.engine.base import PhotoRef

MIN_COMPARABLES = 5
MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 8_000_000

ItemType = Literal["loose_stone", "ring", "earrings", "necklace", "pendant", "bracelet",
                   "brooch", "other"]


# ── Requests ─────────────────────────────────────────────────────────────────


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


class MetalDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Optional[str] = Field(default=None, max_length=32, description="e.g. gold, platinum.")
    purity: Optional[str] = Field(default=None, max_length=16, description="e.g. 18k, 950.")
    color: Optional[str] = Field(default=None, max_length=16)
    weight_grams: Optional[float] = Field(default=None, gt=0, le=5000)


class ItemDetails(BaseModel):
    """What you already know about the item. Every field is optional; all are passed to the scan."""

    model_config = ConfigDict(extra="forbid")

    item_type: Optional[ItemType] = None
    metal: Optional[MetalDetails] = None
    brand: Optional[str] = Field(default=None, max_length=64, description="Maker or house.")
    signed: Optional[bool] = Field(default=None, description="Bears the maker's signature or mark.")
    era: Optional[str] = Field(default=None, max_length=32)
    condition: Optional[str] = Field(default=None, max_length=32)
    lab: Optional[str] = Field(default=None, max_length=16,
                               description="Lab of a report for the item or its main stone.")
    lab_report_number: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=1000,
                                 description="Anything else, e.g. from a catalogue or appraisal.")

    def hints(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ScanRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"photos": [{"url": "https://example.com/ring-front.jpg"},
                            {"url": "https://example.com/ring-inside-band.jpg"}]},
                {"photos": [{"url": "https://example.com/ring-front.jpg"}],
                 "details": {"item_type": "ring", "metal": {"type": "platinum", "weight_grams": 6.4}},
                 "client_reference": "POLICY-88231/ITEM-2"},
            ]
        },
    )

    photos: list[PhotoInput] = Field(
        min_length=1, max_length=MAX_PHOTOS,
        description="1-5 photos of the same item: front, side, and any stamps inside the band or "
                    "on the clasp.",
    )
    details: Optional[ItemDetails] = Field(
        default=None, description="Optional. What you already know, passed to the scan as hints."
    )
    model_version: Optional[str] = Field(
        default=None, max_length=64,
        description="Pin a scan model version (overrides your account's pin).",
    )
    client_reference: Optional[str] = Field(default=None, max_length=128,
                                            description="Your own identifier, echoed back.")

    def photo_refs(self) -> list[PhotoRef]:
        return [p.to_ref() for p in self.photos]

    def public_inputs(self) -> dict[str, Any]:
        """The request as stored and echoed: photo metadata instead of image data."""
        data = self.model_dump(mode="json", exclude={"photos"})
        data["photos"] = [ref.public() for ref in self.photo_refs()]
        return data


# ── Responses ────────────────────────────────────────────────────────────────


class Alternative(BaseModel):
    label: str
    probability: float = Field(ge=0, le=1)


class IdentificationOut(BaseModel):
    status: Literal["identified", "unidentified", "processing"]
    summary: Optional[str] = Field(default=None, description="The preliminary identification.")
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    observations: list[str] = Field(default_factory=list, description="What the photos show.")
    alternatives: list[Alternative] = Field(default_factory=list,
                                            description="What else it could be, with probability.")
    open_questions: list[str] = Field(default_factory=list,
                                      description="What the photos cannot settle.")
    next_evidence: list[str] = Field(default_factory=list,
                                     description="The evidence or testing that would settle them.")


class PriceContextOut(BaseModel):
    status: Literal["provided", "withheld", "processing"]
    currency: str = "USD"
    low: Optional[int] = None
    high: Optional[int] = None
    basis: Optional[str] = Field(default=None,
                                 description="What the figure represents, e.g. retail replacement.")
    explanation: Optional[str] = None
    withheld_reason: Optional[str] = Field(default=None, description="Why there is no price.")

    @model_validator(mode="after")
    def _shape(self) -> "PriceContextOut":
        if self.status == "provided":
            if self.low is None or self.high is None or self.low > self.high:
                raise ValueError("a provided price needs low <= high")
        elif self.low is not None or self.high is not None:
            raise ValueError(f"a {self.status} price must not carry figures")
        return self


class ComparableOut(BaseModel):
    source: str
    title: Optional[str] = None
    url: Optional[str] = None
    photo_url: Optional[str] = None
    price: float
    currency: str
    price_type: Literal["asking", "realized"] = Field(
        description="'realized' = completed sale; 'asking' = list price, not a transaction."
    )
    observed_on: Optional[dt.date] = None
    differences: list[str] = Field(default_factory=list,
                                   description="How it differs from the scanned item.")


class ComparablesOut(BaseModel):
    status: Literal["found", "none_found", "processing"]
    total: int = 0
    realized: int = 0
    asking: int = 0
    items: list[ComparableOut] = Field(default_factory=list)


class ScanResponse(BaseModel):
    object: Literal["scan"] = "scan"
    scan_id: str
    created_at: dt.datetime
    status: Literal["completed", "processing"] = Field(
        description="processing: the scan is still running; retrieve it with GET /scans/{scan_id}."
    )
    product: Literal["scan"] = "scan"
    model_version: Optional[str] = Field(default=None, description="The scan model that produced this.")
    synthetic_data: bool = Field(description="True in the sandbox: example results, not market values.")
    client_reference: Optional[str] = None
    inputs: dict[str, Any] = Field(description="The request as received, with photo metadata.")
    identification: IdentificationOut
    price_context: PriceContextOut
    comparables: ComparablesOut
    warnings: list[str] = Field(default_factory=list)
    batch_job_id: Optional[str] = None

    @model_validator(mode="after")
    def _floor(self) -> "ScanResponse":
        if self.price_context.status == "provided" and self.comparables.total < MIN_COMPARABLES:
            raise ValueError(f"a price requires at least {MIN_COMPARABLES} comparables")
        return self


class BatchJobResponse(BaseModel):
    object: Literal["batch_job"] = "batch_job"
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    created_at: dt.datetime
    started_at: Optional[dt.datetime] = None
    completed_at: Optional[dt.datetime] = None
    model_version: Optional[str] = None
    total_rows: int
    processed_rows: int
    priced_rows: int
    unpriced_rows: int
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


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
