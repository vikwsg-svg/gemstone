"""HTTP routes for /enterprise/v1. Every tenant-owned query filters on the caller's tenant_id."""

import hashlib
from datetime import date, timedelta
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    Path,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from enterprise.auth import Principal, get_principal, require_scope
from enterprise.batch import decode_csv, parse_rows, process_batch_job
from enterprise.deps import AppState, get_db, get_state
from enterprise.errors import ApiError
from enterprise.idempotency import run_idempotent
from enterprise.ids import new_id
from enterprise.metering import usage_for
from enterprise.models import BatchJob, Valuation
from enterprise.schemas import (
    BatchJobResponse,
    ErrorResponse,
    ModelsResponse,
    UsageResponse,
    ValuationBasis,
    ValuationRequest,
    ValuationResponse,
)
from enterprise.valuation.service import ValuationService

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    403: {"model": ErrorResponse, "description": "Key lacks the required product scope."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
_ENGINE_ERRORS = {
    503: {"model": ErrorResponse, "description": "Pricing engine unavailable."},
}
_IDEMPOTENCY_HEADER_DOC = (
    "Optional. Retrying a POST with the same key and body returns the original response "
    "(with `Idempotent-Replayed: true`) instead of creating and billing a second valuation. "
    "Keys are scoped to your account and kept for 24 hours."
)


def _replay_headers(replayed: bool) -> dict:
    return {"Idempotent-Replayed": "true"} if replayed else {}


# ── Valuations ───────────────────────────────────────────────────────────────


@router.post(
    "/valuations",
    status_code=201,
    response_model=ValuationResponse,
    responses={**_ERRORS, **_ENGINE_ERRORS},
    tags=["Comps"],
    summary="Value a piece of jewelry or a loose stone",
    description=(
        "Values a finished piece (ring, earrings, necklace, pendant, bracelet, brooch) or a loose "
        "diamond or gemstone. Returns a low/mid/high price band in the requested valuation basis "
        "(also per carat for loose stones), a Raw/Make/Market breakdown for pieces, the "
        "comparables used, a confidence score with reason codes, and scenario pricing for unknown "
        "natural-vs-lab-grown, treatment or origin. **If fewer than 5 comparables qualify, no "
        "price is returned**: `status` is `insufficient_data` and the closest comps are listed. "
        "Every valuation is stored and retrievable by `valuation_id`."
    ),
)
def create_valuation(
    payload: ValuationRequest,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY_HEADER_DOC
    ),
    principal: Principal = Depends(require_scope("comps")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    service = ValuationService(state, db)
    result = run_idempotent(
        db,
        tenant_id=principal.tenant.id,
        endpoint="POST /valuations",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
        handler=lambda: (201, service.create(principal, payload)),
        now=state.clock(),
        ttl=timedelta(hours=state.settings.idempotency_ttl_hours),
    )
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=_replay_headers(result.replayed),
    )


@router.get(
    "/valuations/{valuation_id}",
    response_model=ValuationResponse,
    responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
    tags=["Comps"],
    summary="Retrieve a stored valuation (audit trail)",
)
def get_valuation(
    valuation_id: str = Path(max_length=40),
    principal: Principal = Depends(require_scope("comps")),
    db: Session = Depends(get_db),
) -> JSONResponse:
    row = db.scalars(
        select(Valuation).where(
            Valuation.id == valuation_id, Valuation.tenant_id == principal.tenant.id
        )
    ).first()
    if row is None:
        # 404 (not 403) for other tenants' ids, so existence is not revealed.
        raise ApiError(404, "valuation_not_found", "No valuation with this id exists.")
    return JSONResponse(content=row.result)


# ── Batch ────────────────────────────────────────────────────────────────────


def _job_body(job: BatchJob, root_path: str) -> dict:
    body = BatchJobResponse(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        valuation_basis=job.valuation_basis,
        model_version=job.model_version,
        total_rows=job.total_rows,
        processed_rows=job.processed_rows,
        priced_rows=job.priced_rows,
        insufficient_rows=job.insufficient_rows,
        failed_rows=job.failed_rows,
        results_url=(
            f"{root_path}/valuations/batch/{job.id}/results" if job.status == "completed" else None
        ),
        error=job.error,
    )
    return body.model_dump(mode="json")


def _get_job(db: Session, principal: Principal, job_id: str) -> BatchJob:
    job = db.scalars(
        select(BatchJob).where(BatchJob.id == job_id, BatchJob.tenant_id == principal.tenant.id)
    ).first()
    if job is None:
        raise ApiError(404, "batch_job_not_found", "No batch job with this id exists.")
    return job


@router.post(
    "/valuations/batch",
    status_code=202,
    response_model=BatchJobResponse,
    responses={
        **_ERRORS,
        **_ENGINE_ERRORS,
        413: {"model": ErrorResponse, "description": "File too large."},
    },
    tags=["Comps"],
    summary="Submit a CSV of pieces and stones for batch valuation",
    description=(
        "Upload a CSV (multipart field `file`), one item per row. Columns: `row_id`, `item_type` "
        "(default `loose_stone`), `valuation_basis`; center or loose stone: `variety`, `species`, "
        "`carat`, `shape`, `color_grade`, `clarity`, `cut_grade`, `treatment`, `origin`, "
        "`growth`, `lab`, `lab_report_number`, `length_mm`, `width_mm`, `depth_mm`; side stones: "
        "`side_variety`, `side_carat_total`, `side_count`, `side_growth`; metal: `metal_type`, "
        "`metal_purity`, `metal_color`, `metal_weight_grams`; piece: `brand`, `signed`, `era`, "
        "`condition`. The header must include `variety`, `item_type` or `metal_type`. "
        "The job runs asynchronously; poll the status endpoint, then download the results CSV. "
        "The whole batch is pinned to one model version."
    ),
)
def create_batch(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(description="CSV file, UTF-8."),
    valuation_basis: Optional[ValuationBasis] = Form(
        default=None, description="Default basis for rows without a valuation_basis column."
    ),
    model_version: Optional[str] = Form(default=None, max_length=64),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY_HEADER_DOC
    ),
    principal: Principal = Depends(require_scope("comps")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    limit = state.settings.batch_max_bytes
    raw = file.file.read(limit + 1)
    if len(raw) > limit:
        raise ApiError(413, "file_too_large", f"The CSV must be at most {limit} bytes.")
    text = decode_csv(raw)
    rows = parse_rows(
        text,
        default_basis=valuation_basis,
        model_version=None,
        max_rows=state.settings.batch_max_rows,
    )
    if not rows:
        raise ApiError(422, "invalid_csv", "The CSV has a header but no rows.")

    service = ValuationService(state, db)
    resolved, _ = service.resolve_model(principal.tenant, model_version)
    service.engine_for(principal.tenant)  # fail fast if the live engine is not configured

    def handler() -> tuple[int, dict]:
        job = BatchJob(
            id=new_id("bat"),
            tenant_id=principal.tenant.id,
            api_key_id=principal.api_key.id,
            status="queued",
            valuation_basis=valuation_basis,
            model_version=resolved.id,
            total_rows=len(rows),
            input_csv=text,
            created_at=state.clock(),
        )
        db.add(job)
        db.flush()
        return 202, _job_body(job, request.scope.get("root_path", ""))

    result = run_idempotent(
        db,
        tenant_id=principal.tenant.id,
        endpoint="POST /valuations/batch",
        key=idempotency_key,
        payload={
            "csv_sha256": hashlib.sha256(raw).hexdigest(),
            "valuation_basis": valuation_basis,
            "model_version": model_version,
        },
        handler=handler,
        now=state.clock(),
        ttl=timedelta(hours=state.settings.idempotency_ttl_hours),
    )
    job_id = result.body["job_id"]
    if result.replayed:
        # Replays report the job's current state rather than the stale snapshot.
        body = _job_body(_get_job(db, principal, job_id), request.scope.get("root_path", ""))
    else:
        body = result.body
        background.add_task(process_batch_job, state, job_id)
    return JSONResponse(
        status_code=result.status_code, content=body, headers=_replay_headers(result.replayed)
    )


@router.get(
    "/valuations/batch/{job_id}",
    response_model=BatchJobResponse,
    responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
    tags=["Comps"],
    summary="Batch job status",
)
def get_batch(
    request: Request,
    job_id: str = Path(max_length=40),
    principal: Principal = Depends(require_scope("comps")),
    db: Session = Depends(get_db),
) -> JSONResponse:
    job = _get_job(db, principal, job_id)
    return JSONResponse(content=_job_body(job, request.scope.get("root_path", "")))


@router.get(
    "/valuations/batch/{job_id}/results",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}}, "description": "Results CSV, one row per input row."},
        **_ERRORS,
        404: {"model": ErrorResponse, "description": "Not found."},
        409: {"model": ErrorResponse, "description": "Job not completed yet."},
    },
    tags=["Comps"],
    summary="Download batch results as CSV",
)
def get_batch_results(
    job_id: str = Path(max_length=40),
    principal: Principal = Depends(require_scope("comps")),
    db: Session = Depends(get_db),
) -> Response:
    job = _get_job(db, principal, job_id)
    if job.status != "completed" or job.results_csv is None:
        raise ApiError(
            409, "batch_not_completed", f"The job is {job.status}; results are not ready."
        )
    return Response(
        content=job.results_csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job.id}-results.csv"'},
    )


# ── Account ──────────────────────────────────────────────────────────────────


@router.get(
    "/models",
    response_model=ModelsResponse,
    responses=_ERRORS,
    tags=["Comps"],
    summary="List model versions available for pinning",
)
def list_models(
    principal: Principal = Depends(require_scope("comps")),
    state: AppState = Depends(get_state),
) -> ModelsResponse:
    return ModelsResponse(
        default_model_version=state.registry.default.id,
        tenant_pinned_model_version=principal.tenant.pinned_model_version,
        data=[
            {
                "id": v.id,
                "engine_version": v.engine_version,
                "released": v.released,
                "status": v.status,
                "similarity_threshold": v.similarity_threshold,
                "notes": v.notes,
            }
            for v in state.registry.available()
        ],
    )


@router.get(
    "/usage",
    response_model=UsageResponse,
    responses=_ERRORS,
    tags=["Account"],
    summary="Daily valuation counts for your account",
)
def get_usage(
    start_date: Optional[date] = Query(default=None, description="Inclusive. Default: 29 days before end_date."),
    end_date: Optional[date] = Query(default=None, description="Inclusive, UTC. Default: today."),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> UsageResponse:
    end = end_date or state.clock().date()
    start = start_date or end - timedelta(days=29)
    if start > end:
        raise ApiError(422, "invalid_date_range", "start_date must be on or before end_date.")
    if (end - start).days > 366:
        raise ApiError(422, "invalid_date_range", "The date range may span at most 366 days.")
    rows = usage_for(db, principal.tenant.id, start, end)
    return UsageResponse(
        tenant_id=principal.tenant.id,
        start_date=start,
        end_date=end,
        data=[
            {
                "date": r.day,
                "product": r.product,
                "valuations_total": r.valuations_total,
                "valuations_priced": r.valuations_priced,
            }
            for r in rows
        ],
    )
