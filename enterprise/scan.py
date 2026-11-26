"""
The Scan API: today's GemstoneAI scan result, enhanced for businesses.

One call runs the scan (identification + price context) and fetches its
comparables, so the customer gets the whole result at once. Around it the
enterprise layer adds: API keys, audit storage (``ent_scans``), safe retries,
usage metering, batch, a model version stamp (and pin), and one rule:

    No price with fewer than 5 comparables. The price context is withheld
    (with the reason) and the comparables that were found are still returned.

If the scan is still running after a short wait, the response is
``processing`` and ``GET /scans/{scan_id}`` returns the finished result.
Image bytes are passed to the scan and never stored: a scan keeps each
photo's sha256, media type, size and URL.
"""

from typing import Optional

from sqlalchemy.orm import Session

from enterprise.auth import Principal
from enterprise.deps import AppState
from enterprise.engine.base import (
    AnalysisResult,
    PendingAnalysis,
    ScanEngine,
    ScanUnavailable,
    UnsupportedModelVersion,
)
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.metering import record_scan
from enterprise.models import Scan, Tenant
from enterprise.schemas import MIN_COMPARABLES, ScanRequest, ScanResponse

PRODUCT = "scan"
SANDBOX_WARNING = (
    "Sandbox: this is an example scan result; it does not reflect the photos and its prices "
    "are not market values."
)
FLOOR_REASON = (
    f"Fewer than {MIN_COMPARABLES} comparables were found, so no price is given. "
    "The comparables that were found are listed."
)


class ScanService:
    def __init__(self, state: AppState, session: Session):
        self.state = state
        self.session = session

    def engine_for(self, tenant: Tenant) -> ScanEngine:
        if tenant.environment == "sandbox":
            return self.state.sandbox_engine
        if self.state.live_engine is None:
            raise ApiError(503, "scan_unavailable",
                           "The scan service is not configured for this environment.")
        return self.state.live_engine

    # ── engine calls ─────────────────────────────────────────────────────────

    def _wait(self, engine: ScanEngine, result: "AnalysisResult | PendingAnalysis",
              wait_seconds: float) -> "AnalysisResult | PendingAnalysis":
        waited = 0.0
        interval = self.state.settings.scan_poll_interval_seconds
        while isinstance(result, PendingAnalysis) and waited < wait_seconds:
            self.state.sleep(interval)
            waited += interval
            result = engine.get(result.analysis_id)
        return result

    @staticmethod
    def _unavailable(exc: Exception) -> ApiError:
        return ApiError(503, "scan_unavailable", "The scan service is temporarily unavailable.")

    # ── bodies ───────────────────────────────────────────────────────────────

    def _completed_body(self, base: dict, engine: ScanEngine, result: AnalysisResult) -> dict:
        comps = engine.comparables(result.analysis_id) if result.identified else []
        price = result.price_context
        price_out = {
            "status": price.status,
            "currency": price.currency,
            "low": int(round(price.low)) if price.status == "provided" and price.low is not None else None,
            "high": int(round(price.high)) if price.status == "provided" and price.high is not None else None,
            "basis": price.basis,
            "explanation": price.explanation,
            "withheld_reason": price.withheld_reason,
        }
        if not result.identified and price_out["status"] == "withheld" and not price_out["withheld_reason"]:
            price_out["withheld_reason"] = "The item could not be identified."
        if price_out["status"] == "provided" and len(comps) < MIN_COMPARABLES:
            price_out.update(status="withheld", low=None, high=None, basis=None,
                             explanation=None, withheld_reason=FLOOR_REASON)

        realized = sum(1 for c in comps if c.price_type == "realized")
        body = {
            **base,
            "status": "completed",
            "model_version": result.model_version,
            "synthetic_data": engine.synthetic,
            "identification": {
                "status": "identified" if result.identified else "unidentified",
                "summary": result.summary,
                "confidence": result.confidence,
                "observations": list(result.observations),
                "alternatives": [dict(a) for a in result.alternatives],
                "open_questions": list(result.open_questions),
                "next_evidence": list(result.next_evidence),
            },
            "price_context": price_out,
            "comparables": {
                "status": "found" if comps else "none_found",
                "total": len(comps),
                "realized": realized,
                "asking": len(comps) - realized,
                "items": [
                    {
                        "source": c.source, "title": c.title, "url": c.url,
                        "photo_url": c.photo_url, "price": c.price, "currency": c.currency,
                        "price_type": c.price_type,
                        "observed_on": c.observed_on.isoformat() if c.observed_on else None,
                        "differences": list(c.differences),
                    }
                    for c in comps
                ],
            },
            "warnings": [SANDBOX_WARNING] if engine.synthetic else [],
        }
        return ScanResponse.model_validate(body).model_dump(mode="json")

    @staticmethod
    def _processing_body(base: dict, engine: ScanEngine) -> dict:
        body = {
            **base,
            "status": "processing",
            "model_version": None,
            "synthetic_data": engine.synthetic,
            "identification": {"status": "processing"},
            "price_context": {"status": "processing"},
            "comparables": {"status": "processing"},
            "warnings": [f"The scan is still running. Retrieve it with GET /scans/{base['scan_id']}."],
        }
        return ScanResponse.model_validate(body).model_dump(mode="json")

    # ── persistence ──────────────────────────────────────────────────────────

    def _finish(self, row: Scan, body: dict) -> None:
        row.status = "completed"
        row.result = body
        row.model_version = body["model_version"]
        row.priced = body["price_context"]["status"] == "provided"
        row.completed_at = self.state.clock()
        record_scan(self.session, tenant_id=row.tenant_id, product=PRODUCT,
                    day=row.completed_at.date(), priced=row.priced)

    def create(
        self,
        principal: Principal,
        request: ScanRequest,
        *,
        batch_job_id: Optional[str] = None,
        batch_row: Optional[int] = None,
        wait_seconds: Optional[float] = None,
    ) -> dict:
        """Run (or start) one scan. Stages rows on the session; the caller commits."""
        tenant = principal.tenant
        engine = self.engine_for(tenant)
        photos = request.photo_refs()
        details = request.details.hints() if request.details else {}
        requested_version = request.model_version or tenant.pinned_model_version
        try:
            result = engine.analyze(photos, details, requested_version)
            wait = self.state.settings.scan_wait_seconds if wait_seconds is None else wait_seconds
            result = self._wait(engine, result, wait)
        except UnsupportedModelVersion as exc:
            source = "request" if request.model_version else "account pin"
            raise ApiError(422, "unknown_model_version",
                           f"Model version {exc.requested!r} ({source}) is not available.",
                           details={"requested": exc.requested, "available": exc.available}) from exc
        except ScanUnavailable as exc:
            raise self._unavailable(exc) from exc

        now = self.state.clock()
        base = {
            "object": "scan",
            "scan_id": new_id("scn"),
            "created_at": now.isoformat(),
            "product": PRODUCT,
            "client_reference": request.client_reference,
            "inputs": request.public_inputs(),
            "batch_job_id": batch_job_id,
        }
        row = Scan(
            id=base["scan_id"], tenant_id=tenant.id, api_key_id=principal.api_key.id,
            product=PRODUCT, status="processing", engine=engine.name, photo_count=len(photos),
            request_payload=base["inputs"], result={}, batch_job_id=batch_job_id,
            batch_row=batch_row, created_at=now,
        )
        self.session.add(row)

        if isinstance(result, PendingAnalysis):
            row.analysis_ref = result.analysis_id
            row.result = self._processing_body(base, engine)
            return row.result

        row.analysis_ref = result.analysis_id
        try:
            body = self._completed_body(base, engine, result)
        except ScanUnavailable as exc:
            self.session.expunge(row)
            raise self._unavailable(exc) from exc
        self._finish(row, body)
        return body

    def refresh(self, row: Scan) -> dict:
        """Complete a processing scan if its analysis has finished. Caller commits."""
        if row.status != "processing" or not row.analysis_ref:
            return row.result
        engine = self.engine_for(self.session.get(Tenant, row.tenant_id))
        try:
            result = engine.get(row.analysis_ref)
            if isinstance(result, PendingAnalysis):
                return row.result
            base = {k: row.result[k] for k in ("object", "scan_id", "created_at", "product",
                                               "client_reference", "inputs", "batch_job_id")}
            body = self._completed_body(base, engine, result)
        except ScanUnavailable:
            return row.result  # still processing as far as the customer can tell
        self._finish(row, body)
        return body
