"""
The Comps valuation service.

Flow for one valuation:
1. Resolve the model version (request pin > tenant pin > default).
2. Pick the engine: sandbox tenants get synthetic data; live tenants get the
   production engine (or a 503 if none is configured, never a silent fallback).
3. Ask the engine for candidates; reject any comp without a valid
   ``source_type`` (realized | asking) or with unusable data.
4. Keep comps at or above the model version's similarity threshold.
5. HARD RULE: fewer than 5 qualifying comps -> status ``insufficient_data``,
   no price, comps still returned.
6. Otherwise convert each comp to the requested valuation basis and take
   similarity- and source-weighted quantiles as the low/mid/high band.
7. Score confidence, price scenarios for unknown treatment/origin.
8. Persist the full record (inputs, comps, model version, output) and meter it.
"""

import math
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from enterprise.auth import Principal
from enterprise.deps import AppState
from enterprise.engine.base import (
    SOURCE_TYPES,
    CompsEngine,
    EngineComp,
    EngineUnavailable,
    StoneQuery,
)
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.metering import record_valuation
from enterprise.models import Tenant, Valuation
from enterprise.schemas import ValuationRequest, ValuationResponse
from enterprise.valuation.confidence import UsedComp, assess
from enterprise.valuation.model_versions import ModelVersion, UnknownModelVersion
from enterprise.valuation.scenarios import build_scenarios

MIN_COMPS = 5  # Hard floor. Not configurable and not part of a model version.
MAX_COMPS_RETURNED_WHEN_INSUFFICIENT = 10
SUPPORTED_CURRENCY = "USD"

SYNTHETIC_WARNING = (
    "Sandbox: prices are computed from synthetic stones and are not market values."
)


def weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    """Weighted quantile with linear interpolation between weight midpoints."""
    if not values:
        raise ValueError("no values")
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    positions = []
    cumulative = 0.0
    for _, w in pairs:
        positions.append((cumulative + w / 2) / total)
        cumulative += w
    if q <= positions[0]:
        return pairs[0][0]
    if q >= positions[-1]:
        return pairs[-1][0]
    for i in range(1, len(pairs)):
        if positions[i] >= q:
            p0, p1 = positions[i - 1], positions[i]
            v0, v1 = pairs[i - 1][0], pairs[i][0]
            return v0 + (v1 - v0) * (q - p0) / (p1 - p0)
    return pairs[-1][0]  # pragma: no cover


def _positive(x: float) -> bool:
    return math.isfinite(x) and x > 0


def _usable(c: EngineComp) -> bool:
    """A comp counts only if it is tagged realized|asking and its numbers are sane."""
    return (
        c.source_type in SOURCE_TYPES
        and c.currency == SUPPORTED_CURRENCY
        and _positive(c.price_total)
        and _positive(c.carat)
        and math.isfinite(c.similarity)
        and 0.0 <= c.similarity <= 1.0
        and (c.adjusted_price_per_carat is None or _positive(c.adjusted_price_per_carat))
    )


@dataclass
class EvaluatedComp:
    comp: EngineComp
    qualified: bool
    basis_factor: float
    basis_price_per_carat: float
    weight: float

    def to_dict(self) -> dict:
        c = self.comp
        return {
            "comp_id": c.comp_id,
            "source_type": c.source_type,
            "source": c.source,
            "date": c.observed_on.isoformat(),
            "currency": c.currency,
            "carat": c.carat,
            "price_total": round(c.price_total, 2),
            "price_per_carat": round(c.price_per_carat, 2),
            "adjusted_price_per_carat": (
                round(c.adjusted_price_per_carat, 2)
                if c.adjusted_price_per_carat is not None
                else None
            ),
            "basis_factor": self.basis_factor,
            "basis_price_per_carat": round(self.basis_price_per_carat, 2),
            "similarity": round(c.similarity, 4),
            "qualified": self.qualified,
            "attributes": dict(c.attributes),
        }

    def used_view(self) -> UsedComp:
        origin = self.comp.attributes.get("origin")
        return UsedComp(
            similarity=self.comp.similarity,
            source_type=self.comp.source_type,
            observed_on=self.comp.observed_on,
            origin=str(origin) if origin is not None else None,
        )


@dataclass
class PricingRun:
    status: str
    price: Optional[dict]
    returned: list[EvaluatedComp]
    used: list[EvaluatedComp]
    considered: int
    rejected: int
    qualified: int


class ValuationService:
    def __init__(self, state: AppState, session: Session):
        self.state = state
        self.session = session

    # ── resolution ───────────────────────────────────────────────────────────

    def resolve_model(self, tenant: Tenant, requested: Optional[str]) -> tuple[ModelVersion, str]:
        try:
            return self.state.registry.resolve(requested, tenant.pinned_model_version)
        except UnknownModelVersion as exc:
            where = "request" if exc.source == "request" else "account pin"
            raise ApiError(
                422,
                "unknown_model_version",
                f"Model version {exc.requested!r} ({where}) is unknown or retired.",
                details={"requested": exc.requested, "source": exc.source,
                         "available": exc.available},
            ) from exc

    def engine_for(self, tenant: Tenant) -> CompsEngine:
        if tenant.environment == "sandbox":
            return self.state.sandbox_engine
        if self.state.live_engine is None:
            raise ApiError(
                503,
                "engine_unavailable",
                "The live pricing engine is not configured for this environment.",
            )
        return self.state.live_engine

    # ── pricing ──────────────────────────────────────────────────────────────

    def _price(
        self, engine: CompsEngine, mv: ModelVersion, query: StoneQuery, basis: str
    ) -> PricingRun:
        try:
            candidates = engine.find_comps(
                query, engine_version=mv.engine_version, limit=mv.candidate_limit
            )
        except EngineUnavailable as exc:
            raise ApiError(
                503, "engine_unavailable", "The pricing engine is temporarily unavailable."
            ) from exc

        valid: list[EngineComp] = []
        rejected = 0
        for c in candidates:
            if not _usable(c):
                rejected += 1
                continue
            valid.append(c)
        valid.sort(key=lambda c: (-c.similarity, c.comp_id))

        evaluated = []
        for c in valid:
            factor = mv.basis_factor(c.source_type, basis)
            evaluated.append(
                EvaluatedComp(
                    comp=c,
                    qualified=c.similarity >= mv.similarity_threshold,
                    basis_factor=factor,
                    basis_price_per_carat=c.effective_price_per_carat * factor,
                    weight=c.similarity * float(mv.source_weights[c.source_type]),
                )
            )
        qualifying = [e for e in evaluated if e.qualified]
        used = qualifying[: mv.max_comps]

        if len(qualifying) < MIN_COMPS:
            near_misses = [e for e in evaluated if not e.qualified]
            returned = (qualifying + near_misses)[:MAX_COMPS_RETURNED_WHEN_INSUFFICIENT]
            return PricingRun(
                status="insufficient_data",
                price=None,
                returned=returned,
                used=qualifying,
                considered=len(candidates),
                rejected=rejected,
                qualified=len(qualifying),
            )

        values = [e.basis_price_per_carat for e in used]
        weights = [e.weight for e in used]
        q_low, q_mid, q_high = mv.band_quantiles
        ppc = {
            "low": weighted_quantile(values, weights, q_low),
            "mid": weighted_quantile(values, weights, q_mid),
            "high": weighted_quantile(values, weights, q_high),
        }
        price = {
            "per_carat": {k: int(round(v)) for k, v in ppc.items()},
            "total": {k: int(round(v * query.carat)) for k, v in ppc.items()},
        }
        return PricingRun(
            status="priced",
            price=price,
            returned=used,
            used=used,
            considered=len(candidates),
            rejected=rejected,
            qualified=len(qualifying),
        )

    def _confidence(self, query: StoneQuery, run: PricingRun) -> dict:
        return assess(
            query=query,
            used=[e.used_view() for e in run.used],
            qualified_count=run.qualified,
            band_per_carat=run.price["per_carat"] if run.price else None,
            today=self.state.clock().date(),
        )

    def evaluate(self, tenant: Tenant, request: ValuationRequest) -> tuple[dict, CompsEngine]:
        """Compute a valuation body (without id/timestamps). Raises ApiError."""
        mv, mv_source = self.resolve_model(tenant, request.model_version)
        engine = self.engine_for(tenant)
        if not engine.supports(mv.engine_version):
            raise ApiError(
                503,
                "model_version_unavailable",
                f"Model version {mv.id} is not served by the current engine.",
            )
        query = request.to_query()
        basis = request.valuation_basis

        primary = self._price(engine, mv, query, basis)
        confidence = self._confidence(query, primary)

        scenarios = []
        for sc in build_scenarios(query):
            run = self._price(engine, mv, sc.query, basis)
            scenarios.append(
                {
                    "scenario_id": sc.scenario_id,
                    "label": sc.label,
                    "assumptions": sc.assumptions,
                    "status": run.status,
                    "price": run.price,
                    "comps_used": len(run.used) if run.status == "priced" else run.qualified,
                    "confidence": self._confidence(sc.query, run),
                }
            )

        warnings = []
        if engine.synthetic:
            warnings.append(SYNTHETIC_WARNING)
        if primary.status == "insufficient_data":
            warnings.append(
                f"Fewer than {MIN_COMPS} comparables qualified, so no price is returned. "
                "The closest comparables are listed for review."
            )
        if primary.rejected:
            warnings.append(
                f"{primary.rejected} candidate comparables were excluded for missing "
                "source type or invalid data."
            )

        used = primary.used if primary.status == "priced" else []
        body = {
            "object": "valuation",
            "status": primary.status,
            "product": "comps",
            "model_version": mv.id,
            "model_version_source": mv_source,
            "engine_version": mv.engine_version,
            "synthetic_data": engine.synthetic,
            "valuation_basis": basis,
            "currency": SUPPORTED_CURRENCY,
            "client_reference": request.client_reference,
            "inputs": request.model_dump(mode="json"),
            "normalized_inputs": query.to_dict(),
            "price": primary.price,
            "confidence": confidence,
            "comps": [e.to_dict() for e in primary.returned],
            "comps_summary": {
                "considered": primary.considered,
                "rejected": primary.rejected,
                "qualified": primary.qualified,
                "used": len(used),
                "realized": sum(1 for e in used if e.comp.source_type == "realized"),
                "asking": sum(1 for e in used if e.comp.source_type == "asking"),
                "min_required": MIN_COMPS,
                "similarity_threshold": mv.similarity_threshold,
            },
            "scenarios": scenarios,
            "warnings": warnings,
        }
        return body, engine

    # ── persistence ──────────────────────────────────────────────────────────

    def create(
        self,
        principal: Principal,
        request: ValuationRequest,
        *,
        batch_job_id: Optional[str] = None,
        batch_row: Optional[int] = None,
    ) -> dict:
        """Evaluate, persist and meter one valuation. Stages rows; the caller commits."""
        body, engine = self.evaluate(principal.tenant, request)
        now = self.state.clock()
        body["valuation_id"] = new_id("val")
        body["created_at"] = now.isoformat()
        body["batch_job_id"] = batch_job_id
        # Validate against the public schema (which re-checks the 5-comp floor)
        # so that what we store is exactly what the contract promises.
        body = ValuationResponse.model_validate(body).model_dump(mode="json")

        self.session.add(
            Valuation(
                id=body["valuation_id"],
                tenant_id=principal.tenant.id,
                api_key_id=principal.api_key.id,
                product="comps",
                status=body["status"],
                valuation_basis=body["valuation_basis"],
                model_version=body["model_version"],
                model_version_source=body["model_version_source"],
                engine=engine.name,
                comps_used=body["comps_summary"]["used"],
                request_payload=body["inputs"],
                result=body,
                batch_job_id=batch_job_id,
                batch_row=batch_row,
                created_at=now,
            )
        )
        record_valuation(
            self.session,
            tenant_id=principal.tenant.id,
            product="comps",
            day=now.date(),
            priced=body["status"] == "priced",
        )
        return body
