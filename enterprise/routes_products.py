"""HTTP routes for Intake and Verify. Each needs its own product scope.

Both products have the same shape: one item at a time (POST, GET), a work queue
(GET with filters, JSON or CSV), the reviewer's final call (POST .../review),
the pilot metrics (GET .../stats), the account's rules (GET/PUT) and a CSV
batch. Webhook events queued by a request are dispatched after the response.
"""

from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, Path, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from enterprise.auth import Principal, require_scope
from enterprise.deps import AppState, get_db, get_state
from enterprise.errors import ApiError
from enterprise.idempotency import run_idempotent
from enterprise.products.common import ProductHandler, ProductService
from enterprise.products.inputs import INTAKE, VERIFY, prepare
from enterprise.products.intake import IntakeRules
from enterprise.products.schemas import (
    IntakeItemRequest,
    IntakeItemResponse,
    IntakeQueue,
    IntakeReviewRequest,
    IntakeStats,
    VerifyCheckRequest,
    VerifyCheckResponse,
    VerifyQueue,
    VerifyReviewRequest,
    VerifyStats,
)
from enterprise.products.verify import ACTIONS, VerifySettings
from enterprise.routes import batch_results, batch_status, get_job, submit_batch
from enterprise.schemas import BatchJobResponse, ErrorResponse
from enterprise.webhooks import dispatch_pending

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    403: {"model": ErrorResponse, "description": "Key lacks the required product scope."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found."}}
_POST_ERRORS = {**_ERRORS, 503: {"model": ErrorResponse, "description": "Scan service unavailable."}}
_IDEMPOTENCY_DOC = ("Optional. Retrying with the same key and body returns the original response "
                    "instead of running and billing again. Kept for 24 hours.")
_CSV_200 = {200: {"content": {"text/csv": {}}, "description": "JSON, or CSV with `format=csv`."}}
_BATCH_DOC = ("Upload a CSV (multipart field `file`), one item per row, with the same columns as "
              "`POST /scans/batch`{extra}. Each row becomes a normal stored record, exactly as if "
              "sent one at a time. Poll the job, then download the results CSV.")
_REVIEW_ERRORS = {**_ERRORS, **_NOT_FOUND,
                  409: {"model": ErrorResponse, "description": "Scan still running."}}
_RESULTS_ERRORS = {200: {"content": {"text/csv": {}}, "description": "Results CSV."},
                   **_ERRORS, **_NOT_FOUND,
                   409: {"model": ErrorResponse, "description": "Job not completed yet."}}


def _dispatch(background: BackgroundTasks, state: AppState, principal: Principal) -> None:
    background.add_task(dispatch_pending, state, principal.tenant.id)


def _create(state: AppState, db: Session, principal: Principal, handler: ProductHandler,
            payload: BaseModel, key: Optional[str]) -> JSONResponse:
    service = ProductService(state, db, handler)

    def run() -> tuple[int, dict]:
        prepared = prepare(handler.product, payload)
        body = service.create(principal, prepared.scan_request, prepared.product_input,
                              reference=prepared.reference, subject_key=prepared.subject_key)
        return (202 if body["status"] == "processing" else 201), body

    result = run_idempotent(db, tenant_id=principal.tenant.id, endpoint=f"POST /{handler.path}",
                            key=key, payload=payload.model_dump(mode="json"), handler=run,
                            now=state.clock(),
                            ttl=timedelta(hours=state.settings.idempotency_ttl_hours))
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


def _put_settings(state: AppState, db: Session, principal: Principal, handler: ProductHandler,
                  config: BaseModel) -> dict:
    saved = ProductService(state, db, handler).put_settings(
        principal.tenant, config.model_dump(mode="json"))
    db.commit()
    return saved


def _queue(state: AppState, db: Session, principal: Principal, handler: ProductHandler,
           fmt: str, **filters) -> Response:
    service = ProductService(state, db, handler)
    if fmt == "csv":
        filters["limit"], filters["offset"] = 10_000, 0
    body = service.queue(principal, **filters)
    if fmt == "csv":
        return Response(service.to_csv(body["data"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{handler.product}-queue.csv"'})
    return JSONResponse(content=body)


def _date_range(state: AppState, start: Optional[date], end: Optional[date]) -> tuple[date, date]:
    end = end or state.clock().date()
    start = start or end - timedelta(days=29)
    if start > end:
        raise ApiError(422, "invalid_date_range", "start_date must be on or before end_date.")
    if (end - start).days > 366:
        raise ApiError(422, "invalid_date_range", "The date range may span at most 366 days.")
    return start, end


def _split(value: Optional[str], allowed: tuple[str, ...], name: str) -> Optional[list[str]]:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    unknown = sorted(set(items) - set(allowed))
    if unknown:
        raise ApiError(422, "invalid_filter", f"Unknown {name}: {', '.join(unknown)}.",
                       details={"allowed": list(allowed)})
    return items


_LIMIT = "Page size, up to 200."
_FORMAT = "json, or csv for a spreadsheet of every match (up to 10,000 rows)."
_REVIEWED = "true: only reviewed; false: only awaiting review; any: both."
Reviewed = Literal["true", "false", "any"]
_REVIEWED_MAP = {"true": True, "false": False, "any": None}


# ── Intake ───────────────────────────────────────────────────────────────────


@router.post("/intake/items", status_code=201, response_model=IntakeItemResponse,
             responses={202: {"model": IntakeItemResponse, "description": "Scan still running."},
                        **_POST_ERRORS},
             tags=["Intake"], summary="Route a consigned item: list, review, lab or decline",
             description="Scans the item, then applies your routing rules (`GET /intake/rules`). "
                         "The decision is logged with its reasons, the rules used and the full scan.")
def create_intake_item(
    payload: IntakeItemRequest,
    background: BackgroundTasks,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("intake")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    _dispatch(background, state, principal)
    return _create(state, db, principal, INTAKE, payload, idempotency_key)


@router.get("/intake/items", response_model=IntakeQueue, responses={**_ERRORS, **_CSV_200},
            tags=["Intake"], summary="The intake queue: decisions, newest first",
            description="Filter by decision, review state, reference or date. `format=csv` "
                        "exports every match.")
def list_intake_items(
    decision: Optional[str] = Query(default=None,
                                    description="Comma-separated: list,review,lab,decline"),
    reviewed: Reviewed = Query(default="any", description=_REVIEWED),
    final_decision: Optional[Literal["list", "review", "lab", "decline"]] = Query(
        default=None, description="Only items whose reviewer chose this."),
    reference: Optional[str] = Query(default=None, max_length=128),
    status: Optional[Literal["completed", "processing"]] = None,
    created_from: Optional[date] = Query(default=None, description="On or after (UTC)."),
    created_to: Optional[date] = Query(default=None, description="On or before (UTC)."),
    limit: int = Query(default=50, ge=1, le=200, description=_LIMIT),
    offset: int = Query(default=0, ge=0),
    format: Literal["json", "csv"] = Query(default="json", description=_FORMAT),
    principal: Principal = Depends(require_scope("intake")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> Response:
    return _queue(state, db, principal, INTAKE, format,
                  outcomes=_split(decision, INTAKE.review_outcomes, "decision"),
                  reviewed=_REVIEWED_MAP[reviewed], review_outcome=final_decision,
                  reference=reference,
                  status=status, created_from=created_from, created_to=created_to,
                  limit=limit, offset=offset)


@router.get("/intake/items/{item_id}", response_model=IntakeItemResponse,
            responses={**_ERRORS, **_NOT_FOUND}, tags=["Intake"],
            summary="Retrieve an intake decision")
def get_intake_item(background: BackgroundTasks, item_id: str = Path(max_length=40),
                    principal: Principal = Depends(require_scope("intake")),
                    db: Session = Depends(get_db),
                    state: AppState = Depends(get_state)) -> JSONResponse:
    _dispatch(background, state, principal)
    return JSONResponse(content=ProductService(state, db, INTAKE).get(principal, item_id))


@router.post("/intake/items/{item_id}/review", response_model=IntakeItemResponse,
             responses=_REVIEW_ERRORS, tags=["Intake"],
             summary="Record the specialist's final decision",
             description="What the specialist actually decided for this item. Every call is kept; "
                         "the stats endpoint uses them to measure how often List-routed items "
                         "needed correcting.")
def review_intake_item(payload: IntakeReviewRequest, item_id: str = Path(max_length=40),
                       principal: Principal = Depends(require_scope("intake")),
                       db: Session = Depends(get_db),
                       state: AppState = Depends(get_state)) -> JSONResponse:
    return JSONResponse(content=ProductService(state, db, INTAKE).review(
        principal, item_id, payload.outcome, payload.note, payload.reviewer))


@router.get("/intake/stats", response_model=IntakeStats, responses=_ERRORS, tags=["Intake"],
            summary="Pilot metrics: decisions, reviews and the List-route correction rate")
def get_intake_stats(
    start_date: Optional[date] = Query(default=None, description="Default: 29 days before end."),
    end_date: Optional[date] = Query(default=None, description="Inclusive, UTC. Default: today."),
    principal: Principal = Depends(require_scope("intake")),
    db: Session = Depends(get_db), state: AppState = Depends(get_state),
) -> dict:
    start, end = _date_range(state, start_date, end_date)
    return ProductService(state, db, INTAKE).stats(principal, start, end)


@router.get("/intake/rules", response_model=IntakeRules, responses=_ERRORS, tags=["Intake"],
            summary="Your routing rules")
def get_intake_rules(principal: Principal = Depends(require_scope("intake")),
                     db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return ProductService(state, db, INTAKE).settings(principal.tenant)


@router.put("/intake/rules", response_model=IntakeRules, responses=_ERRORS, tags=["Intake"],
            summary="Set your routing rules")
def put_intake_rules(rules: IntakeRules, principal: Principal = Depends(require_scope("intake")),
                     db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return _put_settings(state, db, principal, INTAKE, rules)


@router.post("/intake/items/batch", status_code=202, response_model=BatchJobResponse,
             responses={**_POST_ERRORS,
                        413: {"model": ErrorResponse, "description": "File too large."}},
             tags=["Intake"], summary="Route a CSV of consigned items in batch",
             description=_BATCH_DOC.format(extra=", plus `reference` (default `row_id`) and "
                                                 "`consignor_description`"))
def create_intake_batch(
    request: Request, background: BackgroundTasks,
    file: UploadFile = File(description="CSV file, UTF-8."),
    model_version: Optional[str] = Form(default=None, max_length=64),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("intake")),
    db: Session = Depends(get_db), state: AppState = Depends(get_state),
) -> JSONResponse:
    return submit_batch("intake", request, background, file, model_version, idempotency_key,
                        principal, db, state)


@router.get("/intake/items/batch/{job_id}", response_model=BatchJobResponse,
            responses={**_ERRORS, **_NOT_FOUND}, tags=["Intake"], summary="Intake batch status")
def get_intake_batch(request: Request, job_id: str = Path(max_length=40),
                     principal: Principal = Depends(require_scope("intake")),
                     db: Session = Depends(get_db)) -> JSONResponse:
    return batch_status(request, get_job(db, principal, job_id, "intake"))


@router.get("/intake/items/batch/{job_id}/results", response_class=Response,
            responses=_RESULTS_ERRORS, tags=["Intake"],
            summary="Download intake batch results as CSV")
def get_intake_batch_results(job_id: str = Path(max_length=40),
                             principal: Principal = Depends(require_scope("intake")),
                             db: Session = Depends(get_db)) -> Response:
    return batch_results(get_job(db, principal, job_id, "intake"))


# ── Verify ───────────────────────────────────────────────────────────────────


@router.post("/verify/checks", status_code=201, response_model=VerifyCheckResponse,
             responses={202: {"model": VerifyCheckResponse, "description": "Scan still running."},
                        **_POST_ERRORS},
             tags=["Verify"], summary="Risk-check a listing, claim or appraisal",
             description="Scans the item, then checks the subject's stated value and photos against "
                         "the scan and this account's history. Returns a 0-100 risk score, the checks "
                         "that fired with their evidence, and a suggested action. A risk signal for "
                         "review, not an authentication or certification.")
def create_verify_check(
    payload: VerifyCheckRequest,
    background: BackgroundTasks,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("verify")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    _dispatch(background, state, principal)
    return _create(state, db, principal, VERIFY, payload, idempotency_key)


@router.get("/verify/checks", response_model=VerifyQueue, responses={**_ERRORS, **_CSV_200},
            tags=["Verify"], summary="The review queue: checks ranked by risk",
            description="Default: checks awaiting review whose action is not `allow`, highest risk "
                        "first. Pass `reviewed`, `action` or `order` to see everything else. "
                        "`format=csv` exports every match.")
def list_verify_checks(
    action: Optional[str] = Query(
        default="request_lab_report,manual_review,hold",
        description="Comma-separated: allow,request_lab_report,manual_review,hold. "
                    "Empty for every action."),
    reviewed: Reviewed = Query(default="false", description=_REVIEWED),
    outcome: Optional[Literal["confirmed", "cleared"]] = Query(
        default=None, description="Only checks a reviewer marked this way."),
    min_score: Optional[int] = Query(default=None, ge=0, le=100),
    seller_id: Optional[str] = Query(default=None, max_length=128),
    reference: Optional[str] = Query(default=None, max_length=128,
                                     description="The subject's external_id."),
    status: Optional[Literal["completed", "processing"]] = None,
    created_from: Optional[date] = Query(default=None, description="On or after (UTC)."),
    created_to: Optional[date] = Query(default=None, description="On or before (UTC)."),
    order: Literal["risk", "newest"] = Query(default="risk"),
    limit: int = Query(default=50, ge=1, le=200, description=_LIMIT),
    offset: int = Query(default=0, ge=0),
    format: Literal["json", "csv"] = Query(default="json", description=_FORMAT),
    principal: Principal = Depends(require_scope("verify")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> Response:
    return _queue(state, db, principal, VERIFY, format,
                  outcomes=_split(action, ACTIONS, "action"), reviewed=_REVIEWED_MAP[reviewed],
                  review_outcome=outcome, min_score=min_score, subject_key=seller_id,
                  reference=reference, status=status, created_from=created_from,
                  created_to=created_to, order=order, limit=limit, offset=offset)


@router.get("/verify/checks/{check_id}", response_model=VerifyCheckResponse,
            responses={**_ERRORS, **_NOT_FOUND}, tags=["Verify"], summary="Retrieve a risk check")
def get_verify_check(background: BackgroundTasks, check_id: str = Path(max_length=40),
                     principal: Principal = Depends(require_scope("verify")),
                     db: Session = Depends(get_db),
                     state: AppState = Depends(get_state)) -> JSONResponse:
    _dispatch(background, state, principal)
    return JSONResponse(content=ProductService(state, db, VERIFY).get(principal, check_id))


@router.post("/verify/checks/{check_id}/review", response_model=VerifyCheckResponse,
             responses=_REVIEW_ERRORS, tags=["Verify"],
             summary="Record the reviewer's verdict: confirmed or cleared",
             description="Every call is kept. The stats endpoint uses them to measure the "
                         "precision of the flags.")
def review_verify_check(payload: VerifyReviewRequest, check_id: str = Path(max_length=40),
                        principal: Principal = Depends(require_scope("verify")),
                        db: Session = Depends(get_db),
                        state: AppState = Depends(get_state)) -> JSONResponse:
    return JSONResponse(content=ProductService(state, db, VERIFY).review(
        principal, check_id, payload.outcome, payload.note, payload.reviewer))


@router.get("/verify/stats", response_model=VerifyStats, responses=_ERRORS, tags=["Verify"],
            summary="Pilot metrics: flags, reviews and the precision of the top 5%")
def get_verify_stats(
    start_date: Optional[date] = Query(default=None, description="Default: 29 days before end."),
    end_date: Optional[date] = Query(default=None, description="Inclusive, UTC. Default: today."),
    principal: Principal = Depends(require_scope("verify")),
    db: Session = Depends(get_db), state: AppState = Depends(get_state),
) -> dict:
    start, end = _date_range(state, start_date, end_date)
    return ProductService(state, db, VERIFY).stats(principal, start, end)


@router.get("/verify/settings", response_model=VerifySettings, responses=_ERRORS, tags=["Verify"],
            summary="Your risk thresholds")
def get_verify_settings(principal: Principal = Depends(require_scope("verify")),
                        db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return ProductService(state, db, VERIFY).settings(principal.tenant)


@router.put("/verify/settings", response_model=VerifySettings, responses=_ERRORS, tags=["Verify"],
            summary="Set your risk thresholds")
def put_verify_settings(cfg: VerifySettings, principal: Principal = Depends(require_scope("verify")),
                        db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return _put_settings(state, db, principal, VERIFY, cfg)


@router.post("/verify/checks/batch", status_code=202, response_model=BatchJobResponse,
             responses={**_POST_ERRORS,
                        413: {"model": ErrorResponse, "description": "File too large."}},
             tags=["Verify"], summary="Screen a CSV of listings or claims in batch",
             description=_BATCH_DOC.format(extra=", plus `external_id` (default `row_id`), "
                                                 "`subject_type` (default `listing`), `seller_id`, "
                                                 "`stated_value` and `currency`"))
def create_verify_batch(
    request: Request, background: BackgroundTasks,
    file: UploadFile = File(description="CSV file, UTF-8."),
    model_version: Optional[str] = Form(default=None, max_length=64),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("verify")),
    db: Session = Depends(get_db), state: AppState = Depends(get_state),
) -> JSONResponse:
    return submit_batch("verify", request, background, file, model_version, idempotency_key,
                        principal, db, state)


@router.get("/verify/checks/batch/{job_id}", response_model=BatchJobResponse,
            responses={**_ERRORS, **_NOT_FOUND}, tags=["Verify"], summary="Verify batch status")
def get_verify_batch(request: Request, job_id: str = Path(max_length=40),
                     principal: Principal = Depends(require_scope("verify")),
                     db: Session = Depends(get_db)) -> JSONResponse:
    return batch_status(request, get_job(db, principal, job_id, "verify"))


@router.get("/verify/checks/batch/{job_id}/results", response_class=Response,
            responses=_RESULTS_ERRORS, tags=["Verify"],
            summary="Download verify batch results as CSV")
def get_verify_batch_results(job_id: str = Path(max_length=40),
                             principal: Principal = Depends(require_scope("verify")),
                             db: Session = Depends(get_db)) -> Response:
    return batch_results(get_job(db, principal, job_id, "verify"))
