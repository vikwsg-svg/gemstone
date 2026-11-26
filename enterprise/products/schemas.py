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


class _ProductResponse(BaseModel):
    id: str
    created_at: dt.datetime
    status: Literal["completed", "processing"] = Field(
        description="processing: the scan is still running; retrieve this record later."
    )
    reference: Optional[str] = None
    scan: ScanResponse = Field(description="The full scan result the decision is based on.")


class IntakeItemResponse(_ProductResponse):
    object: Literal["intake_item"] = "intake_item"
    routing: Union[IntakeRouting, ProcessingBlock]


class VerifyCheckResponse(_ProductResponse):
    object: Literal["verify_check"] = "verify_check"
    risk: Union[VerifyRisk, ProcessingBlock]
