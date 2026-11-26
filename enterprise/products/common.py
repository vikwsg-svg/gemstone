"""
Shared machinery for the products built on the scan: Intake and Verify.

Each product is the scan result plus one layer of rules:

    request -> ScanService.create(product=...)   (metered under the product)
            -> handler.evaluate(scan result, product input, account settings)
            -> stored ProductRecord: the product's block plus the full scan

If the scan is still running, the record is ``processing`` and is completed
the next time it is retrieved. Every result embeds the scan it was based on,
so the evidence travels with the decision.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from enterprise.auth import Principal
from enterprise.deps import AppState
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.models import PhotoFingerprint, ProductRecord, ProductSettings, Scan, Tenant
from enterprise.scan import ScanService
from enterprise.schemas import ScanRequest


@dataclass
class EvalContext:
    scan: dict  # a completed scan result
    product_input: dict
    settings: dict
    record_id: str
    tenant_id: str
    reference: Optional[str]
    subject_key: Optional[str]
    photo_hashes: list[str]
    session: Session
    now: datetime


class ProductHandler(ABC):
    product: str  # usage and scope name
    object_name: str  # "intake_item", "verify_check"
    id_prefix: str
    result_key: str  # the key holding the product's block in the response
    settings_model: type[BaseModel]

    @abstractmethod
    def evaluate(self, ctx: EvalContext) -> tuple[dict, Optional[str], Optional[int]]:
        """Return (product block, outcome label, score)."""

    def processing_block(self) -> dict:
        return {"status": "processing"}


def _photo_hashes(scan: dict) -> list[str]:
    return [p["sha256"] for p in scan.get("inputs", {}).get("photos", []) if p.get("sha256")]


class ProductService:
    def __init__(self, state: AppState, session: Session, handler: ProductHandler):
        self.state = state
        self.session = session
        self.handler = handler
        self.scans = ScanService(state, session)

    # ── settings ─────────────────────────────────────────────────────────────

    def settings(self, tenant: Tenant) -> dict:
        row = self.session.get(ProductSettings, (tenant.id, self.handler.product))
        stored = row.config if row else {}
        return self.handler.settings_model.model_validate(stored).model_dump(mode="json")

    def put_settings(self, tenant: Tenant, config: dict) -> dict:
        try:
            validated = self.handler.settings_model.model_validate(config).model_dump(mode="json")
        except ValidationError as exc:
            raise ApiError(422, "invalid_settings", "The settings failed validation.",
                           details=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
                           ) from exc
        row = self.session.get(ProductSettings, (tenant.id, self.handler.product))
        if row is None:
            row = ProductSettings(tenant_id=tenant.id, product=self.handler.product,
                                  config=validated, updated_at=self.state.clock())
            self.session.add(row)
        else:
            row.config = validated
            row.updated_at = self.state.clock()
        return validated

    # ── records ──────────────────────────────────────────────────────────────

    def _envelope(self, record: ProductRecord, status: str, block: dict, scan: dict) -> dict:
        return {
            "object": self.handler.object_name,
            "id": record.id,
            "created_at": record.created_at.isoformat(),
            "status": status,
            "reference": record.reference,
            self.handler.result_key: block,
            "scan": scan,
        }

    def _complete(self, record: ProductRecord, scan: dict, product_input: dict,
                  settings: dict) -> dict:
        block, outcome, score = self.handler.evaluate(EvalContext(
            scan=scan, product_input=product_input, settings=settings, record_id=record.id,
            tenant_id=record.tenant_id, reference=record.reference,
            subject_key=record.subject_key, photo_hashes=_photo_hashes(scan),
            session=self.session, now=self.state.clock(),
        ))
        record.status = "completed"
        record.outcome = outcome
        record.score = score
        record.completed_at = self.state.clock()
        record.result = self._envelope(record, "completed", block, scan)
        return record.result

    def create(self, principal: Principal, scan_request: ScanRequest, product_input: dict, *,
               reference: Optional[str], subject_key: Optional[str] = None) -> dict:
        """Run the scan and the product's rules. Stages rows; the caller commits."""
        tenant = principal.tenant
        settings = self.settings(tenant)
        scan = self.scans.create(principal, scan_request, product=self.handler.product)
        record = ProductRecord(
            id=new_id(self.handler.id_prefix), tenant_id=tenant.id,
            api_key_id=principal.api_key.id, product=self.handler.product,
            scan_id=scan["scan_id"], status="processing", reference=reference,
            subject_key=subject_key,
            request_payload={"product_input": product_input, "settings": settings},
            result={}, created_at=self.state.clock(),
        )
        if scan["status"] == "processing":
            record.result = self._envelope(record, "processing", self.handler.processing_block(), scan)
            body = record.result
        else:
            body = self._complete(record, scan, product_input, settings)
        self.session.add(record)
        for sha in _photo_hashes(scan):
            self.session.add(PhotoFingerprint(tenant_id=tenant.id, sha256=sha,
                                              product=self.handler.product, record_id=record.id,
                                              reference=reference, created_at=self.state.clock()))
        return body

    def get(self, principal: Principal, record_id: str) -> dict:
        record = self.session.scalars(select(ProductRecord).where(
            ProductRecord.id == record_id,
            ProductRecord.tenant_id == principal.tenant.id,
            ProductRecord.product == self.handler.product,
        )).first()
        if record is None:
            # 404 (not 403) for other accounts' ids, so existence is not revealed.
            raise ApiError(404, f"{self.handler.object_name}_not_found",
                           "No record with this id exists.")
        if record.status == "processing":
            scan_row = self.session.get(Scan, record.scan_id)
            scan = self.scans.refresh(scan_row)
            if scan["status"] == "completed":
                payload = record.request_payload
                self._complete(record, scan, payload["product_input"], payload["settings"])
            self.session.commit()
        return record.result


def scan_request_from(photos: list, details: Any, model_version: Optional[str],
                      reference: Optional[str]) -> ScanRequest:
    """The ScanRequest a product sends, built from its own (already validated) request."""
    return ScanRequest(photos=photos, details=details, model_version=model_version,
                       client_reference=reference)
