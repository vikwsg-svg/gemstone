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
from enterprise.models import BatchJob, Scan
from enterprise.scan import ScanService
from enterprise.schemas import (
    AccountResponse,
    BatchJobResponse,
    ErrorResponse,
    ScanRequest,
    ScanResponse,
    UsageResponse,
)

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    403: {"model": ErrorResponse, "description": "Key lacks the required product scope."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
_SCAN_ERRORS = {503: {"model": ErrorResponse, "description": "Scan service unavailable."}}
_IDEMPOTENCY_HEADER_DOC = (
    "Optional. Retrying a POST with the same key and body returns the original response "
    "(with `Idempotent-Replayed: true`) instead of running and billing a second scan. "
    "Keys are scoped to your account and kept for 24 hours."
)


def _replay_headers(replayed: bool) -> dict:
    return {"Idempotent-Replayed": "true"} if replayed else {}


# ── Scans ────────────────────────────────────────────────────────────────────


@router.post(
    "/scans",
    status_code=201,
    response_model=ScanResponse,
    responses={
        202: {"model": ScanResponse, "description": "Accepted; the scan is still running."},
        **_ERRORS,
        **_SCAN_ERRORS,
    },
    tags=["Scan API"],
    summary="Scan a piece of jewelry or a loose stone",
    description=(
        "Today's GemstoneAI scan result, in one response: the identification (observations, "
        "alternatives, open questions, next evidence), the price context, and the comparables "
        "behind it (each an asking price or a realized sale). Send 1-5 photos of one item, plus "
        "any details you already know. **No price with fewer than 5 comparables** or when the "
        "item can't be identified; the reason is always given. If the scan is still running, the "
        "response is `202` with `status: processing`: retrieve it with `GET /scans/{scan_id}`. "
        "Every scan is stored and retrievable."
    ),
)
def create_scan(
    payload: ScanRequest,
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY_HEADER_DOC
    ),
    principal: Principal = Depends(require_scope("scan")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    service = ScanService(state, db)

    def handler() -> tuple[int, dict]:
        body = service.create(principal, payload)
        return (202 if body["status"] == "processing" else 201), body

    result = run_idempotent(
        db,
        tenant_id=principal.tenant.id,
        endpoint="POST /scans",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
        handler=handler,
        now=state.clock(),
        ttl=timedelta(hours=state.settings.idempotency_ttl_hours),
    )
    return JSONResponse(status_code=result.status_code, content=result.body,
                        headers=_replay_headers(result.replayed))


@router.get(
    "/scans/{scan_id}",
    response_model=ScanResponse,
    responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
    tags=["Scan API"],
    summary="Retrieve a scan (audit trail, or the result of a running scan)",
)
def get_scan(
    scan_id: str = Path(max_length=40),
    principal: Principal = Depends(require_scope("scan")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    row = db.scalars(
        select(Scan).where(Scan.id == scan_id, Scan.tenant_id == principal.tenant.id)
    ).first()
    if row is None:
        # 404 (not 403) for other tenants' ids, so existence is not revealed.
        raise ApiError(404, "scan_not_found", "No scan with this id exists.")
    if row.status == "processing":
        body = ScanService(state, db).refresh(row)
        db.commit()
        return JSONResponse(content=body)
    return JSONResponse(content=row.result)


# ── Batch ────────────────────────────────────────────────────────────────────


BATCH_PATHS = {"scan": "/scans/batch", "intake": "/intake/items/batch",
               "verify": "/verify/checks/batch"}


def _job_body(job: BatchJob, root_path: str) -> dict:
    product = job.product or "scan"
    return BatchJobResponse(
        job_id=job.id,
        product=product,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        model_version=job.model_version,
        total_rows=job.total_rows,
        processed_rows=job.processed_rows,
        priced_rows=job.priced_rows,
        unpriced_rows=job.unpriced_rows,
        failed_rows=job.failed_rows,
        results_url=(
            f"{root_path}{BATCH_PATHS[product]}/{job.id}/results"
            if job.status == "completed" else None
        ),
        error=job.error,
    ).model_dump(mode="json")


def get_job(db: Session, principal: Principal, job_id: str, product: str = "scan") -> BatchJob:
    job = db.scalars(
        select(BatchJob).where(BatchJob.id == job_id, BatchJob.tenant_id == principal.tenant.id,
                               BatchJob.product == product)
    ).first()
    if job is None:
        raise ApiError(404, "batch_job_not_found", "No batch job with this id exists.")
    return job


def submit_batch(product: str, request: Request, background: BackgroundTasks, file: UploadFile,
                 model_version: Optional[str], idempotency_key: Optional[str],
                 principal: Principal, db: Session, state: AppState) -> JSONResponse:
    """Create a batch job for any product (shared by the Scan API, Intake and Verify routes)."""
    limit = state.settings.batch_max_bytes
    raw = file.file.read(limit + 1)
    if len(raw) > limit:
        raise ApiError(413, "file_too_large", f"The CSV must be at most {limit} bytes.")
    text = decode_csv(raw)
    rows = parse_rows(text, model_version=model_version, max_rows=state.settings.batch_max_rows,
                      product=product)
    if not rows:
        raise ApiError(422, "invalid_csv", "The CSV has a header but no rows.")
    ScanService(state, db).engine_for(principal.tenant)  # fail fast if scans are not configured

    def handler() -> tuple[int, dict]:
        job = BatchJob(
            id=new_id("bat"),
            tenant_id=principal.tenant.id,
            api_key_id=principal.api_key.id,
            product=product,
            status="queued",
            model_version=model_version or principal.tenant.pinned_model_version,
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
        endpoint=f"POST {BATCH_PATHS[product]}",
        key=idempotency_key,
        payload={"csv_sha256": hashlib.sha256(raw).hexdigest(), "model_version": model_version},
        handler=handler,
        now=state.clock(),
        ttl=timedelta(hours=state.settings.idempotency_ttl_hours),
    )
    job_id = result.body["job_id"]
    if result.replayed:
        # Replays report the job's current state rather than the stale snapshot.
        body = _job_body(get_job(db, principal, job_id, product), request.scope.get("root_path", ""))
    else:
        body = result.body
        background.add_task(process_batch_job, state, job_id)
    return JSONResponse(status_code=result.status_code, content=body,
                        headers=_replay_headers(result.replayed))


@router.post(
    "/scans/batch",
    status_code=202,
    response_model=BatchJobResponse,
    responses={**_ERRORS, **_SCAN_ERRORS,
               413: {"model": ErrorResponse, "description": "File too large."}},
    tags=["Scan API"],
    summary="Submit a CSV of items to scan in batch",
    description=(
        "Upload a CSV (multipart field `file`), one item per row. Required column: `photo_urls` "
        "(1-5 public https links separated by `|`). Optional columns, passed to the scan as "
        "details: `row_id`, `item_type`, `metal_type`, `metal_purity`, `metal_weight_grams`, "
        "`brand`, `signed`, `era`, `condition`, `lab`, `lab_report_number`, `notes`. The job runs "
        "asynchronously; poll the status endpoint, then download the results CSV."
    ),
)
def create_batch(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(description="CSV file, UTF-8."),
    model_version: Optional[str] = Form(default=None, max_length=64),
    idempotency_key: Optional[str] = Header(
        default=None, alias="Idempotency-Key", description=_IDEMPOTENCY_HEADER_DOC
    ),
    principal: Principal = Depends(require_scope("scan")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    return submit_batch("scan", request, background, file, model_version, idempotency_key,
                        principal, db, state)


@router.get(
    "/scans/batch/{job_id}",
    response_model=BatchJobResponse,
    responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
    tags=["Scan API"],
    summary="Batch job status",
)
def get_batch(
    request: Request,
    job_id: str = Path(max_length=40),
    principal: Principal = Depends(require_scope("scan")),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(content=_job_body(get_job(db, principal, job_id),
                                          request.scope.get("root_path", "")))


@router.get(
    "/scans/batch/{job_id}/results",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}}, "description": "Results CSV, one row per input row."},
        **_ERRORS,
        404: {"model": ErrorResponse, "description": "Not found."},
        409: {"model": ErrorResponse, "description": "Job not completed yet."},
    },
    tags=["Scan API"],
    summary="Download batch results as CSV",
)
def get_batch_results(
    job_id: str = Path(max_length=40),
    principal: Principal = Depends(require_scope("scan")),
    db: Session = Depends(get_db),
) -> Response:
    return batch_results(get_job(db, principal, job_id))


def batch_status(request: Request, job: BatchJob) -> JSONResponse:
    return JSONResponse(content=_job_body(job, request.scope.get("root_path", "")))


def batch_results(job: BatchJob) -> Response:
    if job.status != "completed" or job.results_csv is None:
        raise ApiError(409, "batch_not_completed", f"The job is {job.status}; results are not ready.")
    return Response(
        content=job.results_csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job.id}-results.csv"'},
    )


# ── Account ──────────────────────────────────────────────────────────────────


@router.get("/account", response_model=AccountResponse, responses=_ERRORS, tags=["Account"],
            summary="The account and API key making the request")
def get_account(principal: Principal = Depends(get_principal)) -> AccountResponse:
    tenant = principal.tenant
    return AccountResponse(tenant_id=tenant.id, name=tenant.name, environment=tenant.environment,
                           scopes=sorted(principal.scopes),
                           pinned_model_version=tenant.pinned_model_version,
                           key_id=principal.api_key.id)


@router.get(
    "/usage",
    response_model=UsageResponse,
    responses=_ERRORS,
    tags=["Account"],
    summary="Daily scan counts for your account",
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
        data=[{"date": r.day, "product": r.product, "scans_total": r.scans_total,
               "scans_priced": r.scans_priced} for r in rows],
    )
