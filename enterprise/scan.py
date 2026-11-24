"""
The Scan API: the business version of a GemstoneAI scan result.

A scan is the two steps of the consumer scan, for systems of record:

1. **Identify**: 1-5 photos (and anything the customer already knows) go to the
   identify engine, which returns a preliminary identification with
   observations, alternatives, open questions and the next evidence that
   would settle them. Details the customer states always win over what the
   photos suggest. With no photos, the stated details are the identification.
2. **Price**: the identified item is valued by the comps engine: price range,
   Raw/Make/Market breakdown for pieces, comparables tagged realized or asking,
   confidence with reason codes, and scenarios for unknowns.

Outcomes (``status``): ``priced``; ``insufficient_data`` (fewer than 5
qualifying comps, no price); ``unidentified`` (the item could not be
identified well enough to price, no price); ``processing`` (identification is
still running: retrieve the scan later with GET /scans/{scan_id}).

Every scan is stored with its inputs (photo metadata, never image bytes),
identification, comps, model version and output, and metered once it
completes.
"""

from collections.abc import Mapping
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from enterprise.auth import Principal
from enterprise.deps import AppState
from enterprise.engine.base import (
    Identification,
    IdentificationUnavailable,
    IdentifyEngine,
    PendingIdentification,
    PhotoRef,
)
from enterprise.engine.sandbox_identify import SandboxIdentifier
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.metering import record_scan
from enterprise.models import Scan, Tenant
from enterprise.schemas import ITEM_FIELDS, ItemFields, ScanRequest, ScanResponse
from enterprise.valuation.model_versions import ModelVersion
from enterprise.valuation.service import ValuationService

PRODUCT = "scan"
# Below this identification confidence a scan is not priced (status unidentified).
MIN_IDENTIFICATION_CONFIDENCE = 0.40
SANDBOX_IDENTIFY_WARNING = (
    "Sandbox: identification is simulated; the same photos always return the same "
    "synthetic result."
)
_LOOSE_STONE_DROPS = ("metal", "brand", "signed", "era", "condition", "total_weight_grams")


def format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def merge_item(identified: Mapping[str, Any], stated: Mapping[str, Any]
               ) -> tuple[dict[str, Any], dict[str, str]]:
    """Stated details override identified ones. Returns (item fields, field sources)."""
    merged = {k: v for k, v in identified.items() if k in ITEM_FIELDS and v not in (None, [], {})}
    sources = {k: "identified" for k in merged}
    for key, value in stated.items():
        merged[key] = value
        sources[key] = "stated"
    if merged.get("item_type") == "loose_stone":
        # A stated loose stone drops the setting the photos may have suggested.
        for key in _LOOSE_STONE_DROPS:
            if sources.get(key) == "identified":
                merged.pop(key)
                sources.pop(key)
        stones = merged.get("stones") or []
        if sources.get("stones") == "identified" and len(stones) > 1:
            center = next((s for s in stones if s.get("role") == "center"), None)
            pick = center or max(stones, key=lambda s: s.get("carat", 0) / max(s.get("count", 1), 1))
            merged["stones"] = [{**pick, "role": "center", "count": 1}]
    return merged, sources


class ScanService:
    def __init__(self, state: AppState, session: Session):
        self.state = state
        self.session = session
        self.pricing = ValuationService(state, session)

    # ── engines ──────────────────────────────────────────────────────────────

    def identifier_for(self, tenant: Tenant) -> IdentifyEngine:
        if tenant.environment == "sandbox":
            return self.state.sandbox_identifier or SandboxIdentifier()
        if self.state.live_identifier is None:
            raise ApiError(
                503,
                "identification_unavailable",
                "Photo identification is not configured for this environment. "
                "Send the item's details instead of photos.",
            )
        return self.state.live_identifier

    def _identify(self, identifier: IdentifyEngine, photos: list[PhotoRef],
                  stated: Mapping[str, Any], wait_seconds: float
                  ) -> "Identification | PendingIdentification":
        try:
            result = identifier.identify(photos, stated)
            waited = 0.0
            interval = self.state.settings.identify_poll_interval_seconds
            while isinstance(result, PendingIdentification) and waited < wait_seconds:
                self.state.sleep(interval)
                waited += interval
                result = identifier.poll(result.ref)
            return result
        except IdentificationUnavailable as exc:
            raise ApiError(
                503, "identification_unavailable",
                "The identification service is temporarily unavailable.",
            ) from exc

    # ── bodies ───────────────────────────────────────────────────────────────

    @staticmethod
    def _identification_out(source: str, photos: list[dict], ident: Optional[Identification],
                            status: str, field_sources: Optional[dict] = None) -> dict:
        out: dict[str, Any] = {"source": source, "status": status, "photos": photos,
                               "field_sources": field_sources or {}}
        if ident is not None:
            out.update(
                confidence=ident.confidence,
                summary=ident.summary,
                observations=list(ident.observations),
                alternatives=[dict(a) for a in ident.alternatives],
                open_questions=list(ident.open_questions),
                next_evidence=list(ident.next_evidence),
            )
        return out

    @staticmethod
    def _validated(body: dict) -> dict:
        return ScanResponse.model_validate(body).model_dump(mode="json")

    def _unpriced(self, base: dict, identification: dict, status: str, synthetic: bool,
                  reason: Optional[str], warnings: list[str]) -> dict:
        confidence = None
        if reason is not None:
            confidence = {"score": 0.0, "level": "none",
                          "reasons": [{"code": "unidentified", "message": reason}]}
        return self._validated({
            **base, "status": status, "item_type": None, "synthetic_data": synthetic,
            "identification": identification, "item": None, "price": None, "breakdown": None,
            "confidence": confidence, "comps": [], "comps_summary": None, "scenarios": [],
            "warnings": warnings,
        })

    def _complete(self, *, base: dict, tenant: Tenant, mv: ModelVersion, mv_source: str,
                  ident: Identification, stated: Mapping[str, Any], photos: list[dict],
                  identifier: Optional[IdentifyEngine]) -> dict:
        source = ("photos_and_details" if photos and stated else "photos") if photos else "details"
        synthetic_ident = bool(identifier and identifier.synthetic)
        warnings = [SANDBOX_IDENTIFY_WARNING] if synthetic_ident else []

        low_confidence = bool(photos) and (ident.confidence or 0.0) < MIN_IDENTIFICATION_CONFIDENCE
        if ident.status != "identified" or low_confidence:
            reason = (
                f"The identification is too uncertain to price ({(ident.confidence or 0):.0%} "
                "confident), so no price is returned. See the alternatives and next evidence."
                if ident.status == "identified"
                else "The item could not be identified from the photos, so no price is "
                     "returned. See the next evidence."
            )
            return self._unpriced(base, self._identification_out(source, photos, ident, ident.status),
                                  "unidentified", synthetic_ident, reason, warnings)

        if photos:
            merged, sources = merge_item(ident.item or {}, stated)
        else:
            merged, sources = dict(stated), {k: "stated" for k in stated}
        identification = self._identification_out(source, photos, ident, "identified", sources)
        try:
            fields = ItemFields.model_validate(merged)
            fields.check_complete()
        except ValidationError as exc:
            reason = f"The identified item is incomplete or inconsistent: {format_validation_error(exc)}"
            return self._unpriced(base, identification, "unidentified", synthetic_ident, reason, warnings)
        except ValueError as exc:
            reason = f"The identified item is incomplete or inconsistent: {exc}"
            return self._unpriced(base, identification, "unidentified", synthetic_ident, reason, warnings)

        fragment, _engine = self.pricing.price_item(
            tenant, fields.to_query(), base["valuation_basis"], mv, mv_source,
            identification_confidence=ident.confidence if photos else None,
        )
        body = {**base, **fragment, "identification": identification}
        body["synthetic_data"] = fragment["synthetic_data"] or synthetic_ident
        body["warnings"] = warnings + fragment["warnings"]
        return self._validated(body)

    # ── persistence ──────────────────────────────────────────────────────────

    def _finish(self, row: Scan, body: dict) -> None:
        row.status = body["status"]
        row.result = body
        row.comps_used = body["comps_summary"]["used"] if body.get("comps_summary") else 0
        row.identification_ref = None
        row.completed_at = self.state.clock()
        record_scan(self.session, tenant_id=row.tenant_id, product=PRODUCT,
                    day=row.completed_at.date(), priced=body["status"] == "priced")

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
        mv, mv_source = self.pricing.resolve_model(tenant, request.model_version)
        pricing_engine = self.pricing.engine_for(tenant)  # fail fast before identifying
        photo_refs = request.photo_refs()
        photos = [p.public() for p in photo_refs]
        stated = request.stated()
        identifier = self.identifier_for(tenant) if photo_refs else None
        now = self.state.clock()

        base = {
            "object": "scan",
            "scan_id": new_id("scn"),
            "created_at": now.isoformat(),
            "product": PRODUCT,
            "model_version": mv.id,
            "model_version_source": mv_source,
            "engine_version": mv.engine_version,
            "valuation_basis": request.valuation_basis,
            "currency": "USD",
            "client_reference": request.client_reference,
            "inputs": request.public_inputs(),
            "batch_job_id": batch_job_id,
        }
        row = Scan(
            id=base["scan_id"], tenant_id=tenant.id, api_key_id=principal.api_key.id,
            product=PRODUCT, status="processing", valuation_basis=request.valuation_basis,
            model_version=mv.id, model_version_source=mv_source, engine=pricing_engine.name,
            identifier=identifier.name if identifier else None, photo_count=len(photo_refs),
            request_payload=base["inputs"], result={}, batch_job_id=batch_job_id,
            batch_row=batch_row, created_at=now,
        )

        if identifier is not None:
            wait = self.state.settings.identify_wait_seconds if wait_seconds is None else wait_seconds
            ident = self._identify(identifier, photo_refs, stated, wait)
        else:
            ident = Identification(status="identified", item=stated)

        if isinstance(ident, PendingIdentification):
            body = self._unpriced(
                base,
                self._identification_out("photos_and_details" if stated else "photos", photos,
                                         None, "processing"),
                "processing", identifier.synthetic, None,
                [f"Identification is still running. Retrieve this scan with GET /scans/{base['scan_id']}."],
            )
            row.identification_ref = ident.ref
            row.result = body
            self.session.add(row)
            return body

        body = self._complete(base=base, tenant=tenant, mv=mv, mv_source=mv_source, ident=ident,
                              stated=stated, photos=photos, identifier=identifier)
        self.session.add(row)
        self._finish(row, body)
        return body

    def refresh(self, row: Scan) -> dict:
        """Advance a processing scan if its identification has finished. Caller commits."""
        if row.status != "processing" or not row.identification_ref:
            return row.result
        tenant = self.session.get(Tenant, row.tenant_id)
        identifier = self.identifier_for(tenant)
        try:
            ident = identifier.poll(row.identification_ref)
        except IdentificationUnavailable:
            return row.result  # still processing as far as the customer can tell
        if isinstance(ident, PendingIdentification):
            return row.result

        inputs = row.request_payload
        stated = ItemFields.model_validate({k: inputs.get(k) for k in ITEM_FIELDS
                                            if inputs.get(k) is not None}).stated()
        mv = self.state.registry.get(row.model_version)
        base = {k: row.result[k] for k in (
            "object", "scan_id", "created_at", "product", "model_version", "model_version_source",
            "engine_version", "valuation_basis", "currency", "client_reference", "inputs",
            "batch_job_id")}
        body = self._complete(base=base, tenant=tenant, mv=mv, mv_source=row.model_version_source,
                              ident=ident, stated=stated, photos=inputs.get("photos", []),
                              identifier=identifier)
        self._finish(row, body)
        return body
