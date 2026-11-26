"""Request and response models for Intake and Verify (these generate the OpenAPI spec)."""

import datetime as dt
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from enterprise.products.intake import IntakeRules
from enterprise.products.verify import Subject, VerifySettings
from enterprise.schemas import MAX_PHOTOS, ComparableOut, ItemDetails, PhotoInput, ScanResponse


class _ScanInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photos: list[PhotoInput] = Field(min_length=1, max_length=MAX_PHOTOS,
                                     description="1-5 photos of the same item.")
    details: Optional[ItemDetails] = Field(default=None,
                                           description="What you already know, passed to the scan.")
    model_version: Optional[str] = Field(default=None, max_length=64,
                                         description="Pin a scan model version.")


# ── Requests ─────────────────────────────────────────────────────────────────


class IntakeItemRequest(_ScanInputs):
    consignor_description: Optional[str] = Field(
        default=None, max_length=1000, description="What the consignor says the item is."
    )
    reference: Optional[str] = Field(default=None, max_length=128,
                                     description="Your consignment or item id.")


class VerifyCheckRequest(_ScanInputs):
    subject: Subject = Field(description="The listing, claim or appraisal being checked.")


# ── Responses ────────────────────────────────────────────────────────────────


class ProcessingBlock(BaseModel):
    status: Literal["processing"]


class Reason(BaseModel):
    code: str
    message: str


class IntakeRouting(BaseModel):
    decision: Literal["list", "review", "lab", "decline"]
    reasons: list[Reason]
    rules_applied: IntakeRules
    consignor_description: Optional[str] = None
    prefill: dict[str, Any] = Field(description="Suggested listing fields from the scan.")


class RiskCheck(BaseModel):
    code: str
    ran: bool
    fired: bool
    weight: int
    message: str
    evidence: dict[str, Any]


class VerifyRisk(BaseModel):
    risk_score: int = Field(ge=0, le=100)
    action: Literal["allow", "request_lab_report", "manual_review", "hold"]
    checks: list[RiskCheck]
    subject: Subject
    settings_applied: VerifySettings
    note: str


class Review(BaseModel):
    outcome: str = Field(description="The reviewer's final call.")
    agrees: bool = Field(description="Whether the final call agrees with the product's outcome.")
    note: Optional[str] = None
    reviewer: Optional[str] = None
    reviewed_at: dt.datetime
    previous: list[dict[str, Any]] = Field(default_factory=list,
                                           description="Earlier calls on this record, newest first.")


class _ProductResponse(BaseModel):
    id: str
    created_at: dt.datetime
    status: Literal["completed", "processing"] = Field(
        description="processing: the scan is still running; retrieve this record later."
    )
    reference: Optional[str] = None
    scan: ScanResponse = Field(description="The full scan result the decision is based on.")
    review: Optional[Review] = Field(default=None,
                                     description="The latest final call by a reviewer, if any.")


class IntakeItemResponse(_ProductResponse):
    object: Literal["intake_item"] = "intake_item"
    routing: Union[IntakeRouting, ProcessingBlock]


class VerifyCheckResponse(_ProductResponse):
    object: Literal["verify_check"] = "verify_check"
    risk: Union[VerifyRisk, ProcessingBlock]


# ── Final calls, queues and pilot metrics ────────────────────────────────────


class _ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=2000)
    reviewer: Optional[str] = Field(default=None, max_length=128,
                                    description="Who made the call, e.g. an email or staff id.")


class IntakeReviewRequest(_ReviewRequest):
    outcome: Literal["list", "review", "lab", "decline"] = Field(
        description="What the specialist actually decided for this item.")


class VerifyReviewRequest(_ReviewRequest):
    outcome: Literal["confirmed", "cleared"] = Field(
        description="confirmed: a real problem; cleared: nothing wrong.")


class _QueueRow(BaseModel):
    id: str
    created_at: dt.datetime
    status: Literal["completed", "processing"]
    reference: Optional[str] = None
    scan_id: str
    identified_as: Optional[str] = None
    identification_confidence: Optional[float] = None
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    currency: Optional[str] = None
    review_outcome: Optional[str] = None
    review_agrees: Optional[bool] = None
    reviewed_at: Optional[dt.datetime] = None


class IntakeQueueRow(_QueueRow):
    decision: Optional[Literal["list", "review", "lab", "decline"]] = None
    reason_codes: Optional[list[str]] = None


class VerifyQueueRow(_QueueRow):
    subject_type: Optional[str] = None
    seller_id: Optional[str] = None
    stated_value: Optional[float] = None
    stated_currency: Optional[str] = None
    risk_score: Optional[int] = None
    action: Optional[Literal["allow", "request_lab_report", "manual_review", "hold"]] = None
    fired_checks: Optional[list[str]] = None


class _Queue(BaseModel):
    object: Literal["list"] = "list"
    total: int = Field(description="Records matching the filters.")
    has_more: bool
    next_offset: Optional[int] = None


class IntakeQueue(_Queue):
    data: list[IntakeQueueRow]


class VerifyQueue(_Queue):
    data: list[VerifyQueueRow]


class _Stats(BaseModel):
    start_date: dt.date
    end_date: dt.date
    processing_now: int = Field(description="Records whose scan is still running (any date).")


class IntakeStats(_Stats):
    object: Literal["intake_stats"] = "intake_stats"
    items: int
    by_decision: dict[str, int]
    reviewed: int
    agreement_rate: Optional[float] = Field(
        description="Share of reviewed items where the final call matched the routing.")
    list_routed_reviewed: int
    list_routed_corrected: int
    list_correction_rate: Optional[float] = Field(
        description="Pilot metric: share of reviewed List-routed items a specialist changed.")


class TopFlags(BaseModel):
    share: float
    count: int
    reviewed: int
    confirmed: int
    precision: Optional[float]


class VerifyStats(_Stats):
    object: Literal["verify_stats"] = "verify_stats"
    checks: int
    by_action: dict[str, int]
    flagged: int = Field(description="Checks whose action is not allow.")
    flagged_reviewed: int
    confirmed: int
    cleared: int
    flag_precision: Optional[float] = Field(
        description="Share of reviewed flags confirmed as real problems.")
    top_flags: TopFlags = Field(
        description="Pilot metric: the top 5% of checks by risk score, and how many were confirmed.")
    confirmed_stated_value: dict[str, float] = Field(
        description="Stated value of confirmed problems, by currency.")

