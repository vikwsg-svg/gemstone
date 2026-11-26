"""
Shared machinery for the products built on the scan: Intake and Verify.

Each product is the scan result plus one layer of rules:

    request -> ScanService.create(product=...)   (metered under the product)
            -> handler.evaluate(scan result, product input, account settings)
            -> stored ProductRecord: the product's block plus the full scan

If the scan is still running, the record is ``processing`` and is completed
the next time it is retrieved. Every result embeds the scan it was based on,
so the evidence travels with the decision.

Around each record: a work queue (``list``), the person's final call after
review (``review``, kept with every earlier call), the pilot success metrics
(``stats``) and webhook events, queued in the same transaction as the record.
"""

import csv
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from enterprise.auth import Principal
from enterprise.deps import AppState
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.models import PhotoFingerprint, ProductRecord, ProductSettings, Scan, Tenant
from enterprise.scan import ScanService
from enterprise.schemas import ScanRequest
from enterprise.webhooks import emit


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
    path: str  # "intake/items", "verify/checks"
    result_key: str  # the key holding the product's block in the response
    settings_model: type[BaseModel]
    review_outcomes: tuple[str, ...]  # what a reviewer can record as the final call
    summary_columns: tuple[str, ...]  # product-specific queue / CSV columns

    @abstractmethod
    def evaluate(self, ctx: EvalContext) -> tuple[dict, Optional[str], Optional[int]]:
        """Return (product block, outcome label, score)."""

    @abstractmethod
    def summarize(self, record: ProductRecord, block: dict) -> dict:
        """The product-specific queue columns for a completed record."""

    @abstractmethod
    def agrees(self, record: ProductRecord, review_outcome: str) -> bool:
        """Whether the reviewer's final call agrees with the product's outcome."""

    @abstractmethod
    def stats(self, rows: list[ProductRecord]) -> dict:
        """The pilot success metrics over completed records."""

    @abstractmethod
    def events(self, record: ProductRecord) -> list[str]:
        """Webhook event types for a record that just completed."""

    def processing_block(self) -> dict:
        return {"status": "processing"}


BASE_COLUMNS = ("id", "created_at", "status", "reference", "scan_id", "identified_as",
                "identification_confidence", "price_low", "price_high", "currency")
REVIEW_COLUMNS = ("review_outcome", "review_agrees", "reviewed_at")


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _safe_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection in text from users or engines."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


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
            "created_at": _iso(record.created_at),
            "status": status,
            "reference": record.reference,
            self.handler.result_key: block,
            "scan": scan,
        }

    def _review_block(self, record: ProductRecord) -> Optional[dict]:
        if not record.review:
            return None
        return {k: record.review.get(k) for k in
                ("outcome", "agrees", "note", "reviewer", "reviewed_at", "previous")}

    def body(self, record: ProductRecord) -> dict:
        """The record as the API returns it: the stored result plus the latest review."""
        return {**record.result, "review": self._review_block(record)}

    def summary(self, record: ProductRecord) -> dict:
        result = record.result or {}
        scan = result.get("scan") or {}
        ident = scan.get("identification") or {}
        price = scan.get("price_context") or {}
        row = {
            "id": record.id, "created_at": _iso(record.created_at), "status": record.status,
            "reference": record.reference, "scan_id": record.scan_id,
            "identified_as": ident.get("summary"),
            "identification_confidence": ident.get("confidence"),
            "price_low": price.get("low"), "price_high": price.get("high"),
            "currency": price.get("currency"),
        }
        block = result.get(self.handler.result_key) or {}
        if record.status == "completed":
            row.update(self.handler.summarize(record, block))
        else:
            row.update({c: None for c in self.handler.summary_columns})
        review = record.review or {}
        row.update(review_outcome=record.review_outcome, review_agrees=review.get("agrees"),
                   reviewed_at=_iso(record.reviewed_at))
        return row

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
        data = {"object": self.handler.object_name, "id": record.id,
                "path": f"/{self.handler.path}/{record.id}", "summary": self.summary(record)}
        for event_type in self.handler.events(record):
            emit(self.session, record.tenant_id, event_type, data, self.state.clock())
        return record.result

    def create(self, principal: Principal, scan_request: ScanRequest, product_input: dict, *,
               reference: Optional[str], subject_key: Optional[str] = None,
               batch_job_id: Optional[str] = None, batch_row: Optional[int] = None,
               wait_seconds: Optional[float] = None) -> dict:
        """Run the scan and the product's rules. Stages rows; the caller commits."""
        tenant = principal.tenant
        settings = self.settings(tenant)
        scan = self.scans.create(principal, scan_request, product=self.handler.product,
                                 batch_job_id=batch_job_id, batch_row=batch_row,
                                 wait_seconds=wait_seconds)
        record = ProductRecord(
            id=new_id(self.handler.id_prefix), tenant_id=tenant.id,
            api_key_id=principal.api_key.id, product=self.handler.product,
            scan_id=scan["scan_id"], status="processing", reference=reference,
            subject_key=subject_key,
            request_payload={"product_input": product_input, "settings": settings},
            result={}, created_at=self.state.clock(),
        )
        self.session.add(record)
        for sha in _photo_hashes(scan):
            self.session.add(PhotoFingerprint(tenant_id=tenant.id, sha256=sha,
                                              product=self.handler.product, record_id=record.id,
                                              reference=reference, created_at=self.state.clock()))
        self.session.flush()
        if scan["status"] == "processing":
            record.result = self._envelope(record, "processing", self.handler.processing_block(), scan)
        else:
            self._complete(record, scan, product_input, settings)
        return self.body(record)

    def _record(self, principal: Principal, record_id: str) -> ProductRecord:
        record = self.session.scalars(select(ProductRecord).where(
            ProductRecord.id == record_id,
            ProductRecord.tenant_id == principal.tenant.id,
            ProductRecord.product == self.handler.product,
        )).first()
        if record is None:
            # 404 (not 403) for other accounts' ids, so existence is not revealed.
            raise ApiError(404, f"{self.handler.object_name}_not_found",
                           "No record with this id exists.")
        return record

    def _refresh(self, record: ProductRecord) -> None:
        if record.status != "processing":
            return
        scan = self.scans.refresh(self.session.get(Scan, record.scan_id))
        if scan["status"] == "completed":
            payload = record.request_payload
            self._complete(record, scan, payload["product_input"], payload["settings"])
        self.session.commit()

    def get(self, principal: Principal, record_id: str) -> dict:
        record = self._record(principal, record_id)
        self._refresh(record)
        return self.body(record)

    # ── review: the person's final call ──────────────────────────────────────

    def review(self, principal: Principal, record_id: str, outcome: str,
               note: Optional[str], reviewer: Optional[str]) -> dict:
        record = self._record(principal, record_id)
        self._refresh(record)
        if record.status != "completed":
            raise ApiError(409, "not_completed", "The scan is still running; review it once it "
                                                 "has completed.")
        now = self.state.clock()
        previous = []
        if record.review:
            earlier = {k: v for k, v in record.review.items() if k != "previous"}
            previous = [earlier, *record.review.get("previous", [])]
        record.review_outcome = outcome
        record.reviewed_at = now
        record.review = {"outcome": outcome, "agrees": self.handler.agrees(record, outcome),
                         "note": note, "reviewer": reviewer, "reviewed_at": _iso(now),
                         "previous": previous}
        self.session.commit()
        return self.body(record)

    # ── queue ────────────────────────────────────────────────────────────────

    def queue(self, principal: Principal, *, outcomes: Optional[list[str]] = None,
             status: Optional[str] = None, reviewed: Optional[bool] = None,
             review_outcome: Optional[str] = None, reference: Optional[str] = None,
             subject_key: Optional[str] = None, min_score: Optional[int] = None,
             created_from: Optional[date] = None, created_to: Optional[date] = None,
             order: str = "newest", limit: int = 50, offset: int = 0) -> dict:
        query = select(ProductRecord).where(ProductRecord.tenant_id == principal.tenant.id,
                                            ProductRecord.product == self.handler.product)
        if outcomes:
            query = query.where(ProductRecord.outcome.in_(outcomes))
        if status:
            query = query.where(ProductRecord.status == status)
        if reviewed is True:
            query = query.where(ProductRecord.review_outcome.is_not(None))
        elif reviewed is False:
            query = query.where(ProductRecord.review_outcome.is_(None))
        if review_outcome:
            query = query.where(ProductRecord.review_outcome == review_outcome)
        if reference:
            query = query.where(ProductRecord.reference == reference)
        if subject_key:
            query = query.where(ProductRecord.subject_key == subject_key)
        if min_score is not None:
            query = query.where(ProductRecord.score >= min_score)
        if created_from:
            query = query.where(ProductRecord.created_at >= datetime.combine(
                created_from, time.min, tzinfo=timezone.utc))
        if created_to:
            query = query.where(ProductRecord.created_at < datetime.combine(
                created_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        if order == "risk":
            query = query.order_by(ProductRecord.score.is_(None), ProductRecord.score.desc(),
                                   ProductRecord.created_at.desc(), ProductRecord.id.desc())
        else:
            query = query.order_by(ProductRecord.created_at.desc(), ProductRecord.id.desc())
        rows = self.session.scalars(query.offset(offset).limit(limit)).all()
        has_more = offset + len(rows) < total
        return {"object": "list", "total": total, "has_more": has_more,
                "next_offset": offset + len(rows) if has_more else None,
                "data": [self.summary(r) for r in rows]}

    def columns(self) -> list[str]:
        return [*BASE_COLUMNS, *self.handler.summary_columns, *REVIEW_COLUMNS]

    def to_csv(self, rows: list[dict]) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=self.columns(), lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _safe_cell("|".join(v) if isinstance(v, list) else
                                           ("" if v is None else
                                            str(v).lower() if isinstance(v, bool) else v))
                             for k, v in row.items()})
        return buffer.getvalue()

    # ── pilot metrics ────────────────────────────────────────────────────────

    def stats(self, principal: Principal, start: date, end: date) -> dict:
        rows = self.session.scalars(select(ProductRecord).where(
            ProductRecord.tenant_id == principal.tenant.id,
            ProductRecord.product == self.handler.product,
            ProductRecord.status == "completed",
            ProductRecord.created_at >= datetime.combine(start, time.min, tzinfo=timezone.utc),
            ProductRecord.created_at < datetime.combine(end + timedelta(days=1), time.min,
                                                        tzinfo=timezone.utc),
        )).all()
        processing = self.session.scalar(select(func.count()).select_from(ProductRecord).where(
            ProductRecord.tenant_id == principal.tenant.id,
            ProductRecord.product == self.handler.product,
            ProductRecord.status == "processing")) or 0
        return {"object": f"{self.handler.product}_stats", "start_date": start.isoformat(),
                "end_date": end.isoformat(), "processing_now": processing,
                **self.handler.stats(rows)}


def rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def scan_request_from(photos: list, details: Any, model_version: Optional[str],
                      reference: Optional[str]) -> ScanRequest:
    """The ScanRequest a product sends, built from its own (already validated) request."""
    return ScanRequest(photos=photos, details=details, model_version=model_version,
                       client_reference=reference)
