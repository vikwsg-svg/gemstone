"""HTTP routes for Intake, Estimate and Verify. Each needs its own product scope."""

from datetime import timedelta
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Header, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from enterprise.auth import Principal, require_scope
from enterprise.deps import AppState, get_db, get_state
from enterprise.idempotency import run_idempotent
from enterprise.products.common import ProductHandler, ProductService, scan_request_from
from enterprise.products.estimate import EstimateHandler, EstimatePolicy
from enterprise.products.intake import IntakeHandler, IntakeRules
from enterprise.products.schemas import (
    EstimateLotRequest,
    EstimateResponse,
    IntakeItemRequest,
    IntakeItemResponse,
    VerifyCheckRequest,
    VerifyCheckResponse,
)
from enterprise.products.verify import VerifyHandler, VerifySettings
from enterprise.schemas import ErrorResponse, ItemDetails

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    403: {"model": ErrorResponse, "description": "Key lacks the required product scope."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
_POST_ERRORS = {**_ERRORS, 503: {"model": ErrorResponse, "description": "Scan service unavailable."}}
_IDEMPOTENCY_DOC = ("Optional. Retrying with the same key and body returns the original response "
                    "instead of running and billing again. Kept for 24 hours.")

INTAKE, ESTIMATE, VERIFY = IntakeHandler(), EstimateHandler(), VerifyHandler()


def _create(state: AppState, db: Session, principal: Principal, handler: ProductHandler,
            endpoint: str, payload: BaseModel, key: Optional[str],
            build: Callable[[], tuple[Any, dict, Optional[str], Optional[str]]]) -> JSONResponse:
    service = ProductService(state, db, handler)

    def handler_fn() -> tuple[int, dict]:
        scan_request, product_input, reference, subject_key = build()
        body = service.create(principal, scan_request, product_input,
                              reference=reference, subject_key=subject_key)
        return (202 if body["status"] == "processing" else 201), body

    result = run_idempotent(db, tenant_id=principal.tenant.id, endpoint=endpoint, key=key,
                            payload=payload.model_dump(mode="json"), handler=handler_fn,
                            now=state.clock(),
                            ttl=timedelta(hours=state.settings.idempotency_ttl_hours))
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


def _get(state: AppState, db: Session, principal: Principal, handler: ProductHandler,
         record_id: str) -> JSONResponse:
    return JSONResponse(content=ProductService(state, db, handler).get(principal, record_id))


def _put_settings(state: AppState, db: Session, principal: Principal, handler: ProductHandler,
                  config: BaseModel) -> dict:
    saved = ProductService(state, db, handler).put_settings(
        principal.tenant, config.model_dump(mode="json"))
    db.commit()
    return saved


# ── Intake ───────────────────────────────────────────────────────────────────


@router.post("/intake/items", status_code=201, response_model=IntakeItemResponse,
             responses={202: {"model": IntakeItemResponse, "description": "Scan still running."},
                        **_POST_ERRORS},
             tags=["Intake"], summary="Route a consigned item: list, review, lab or decline",
             description="Scans the item, then applies your routing rules (`GET /intake/rules`). "
                         "The decision is logged with its reasons, the rules used and the full scan.")
def create_intake_item(
    payload: IntakeItemRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("intake")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    def build():
        details = payload.details or ItemDetails()
        if payload.consignor_description and not details.notes:
            details = details.model_copy(update={"notes": f"Consignor says: {payload.consignor_description}"})
        scan_request = scan_request_from(payload.photos, details, payload.model_version, payload.reference)
        product_input = {"consignor_description": payload.consignor_description,
                         "details": payload.details.hints() if payload.details else {}}
        return scan_request, product_input, payload.reference, None

    return _create(state, db, principal, INTAKE, "POST /intake/items", payload, idempotency_key, build)


@router.get("/intake/items/{item_id}", response_model=IntakeItemResponse,
            responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
            tags=["Intake"], summary="Retrieve an intake decision")
def get_intake_item(item_id: str = Path(max_length=40),
                    principal: Principal = Depends(require_scope("intake")),
                    db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> JSONResponse:
    return _get(state, db, principal, INTAKE, item_id)


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


# ── Estimate ─────────────────────────────────────────────────────────────────


@router.post("/estimate/lots", status_code=201, response_model=EstimateResponse,
             responses={202: {"model": EstimateResponse, "description": "Scan still running."},
                        **_POST_ERRORS},
             tags=["Estimate"], summary="Suggest a pre-sale estimate and reserve for a lot",
             description="Scans the lot, then builds the estimate from realized sales among the "
                         "comparables (the middle half of at least 5), rounded to auction increments. "
                         "The reserve follows your policy (`GET /estimate/policy`). With fewer than 5 "
                         "realized sales there is no number: comparables and context only.")
def create_estimate(
    payload: EstimateLotRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("estimate")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    def build():
        scan_request = scan_request_from(payload.photos, payload.details, payload.model_version,
                                         payload.lot_number)
        product_input = {"lot_number": payload.lot_number,
                         "sale": payload.sale.model_dump(mode="json") if payload.sale else None}
        return scan_request, product_input, payload.lot_number, None

    return _create(state, db, principal, ESTIMATE, "POST /estimate/lots", payload, idempotency_key, build)


@router.get("/estimate/lots/{estimate_id}", response_model=EstimateResponse,
            responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
            tags=["Estimate"], summary="Retrieve an estimate")
def get_estimate(estimate_id: str = Path(max_length=40),
                 principal: Principal = Depends(require_scope("estimate")),
                 db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> JSONResponse:
    return _get(state, db, principal, ESTIMATE, estimate_id)


@router.get("/estimate/policy", response_model=EstimatePolicy, responses=_ERRORS, tags=["Estimate"],
            summary="Your reserve policy and rounding increments")
def get_estimate_policy(principal: Principal = Depends(require_scope("estimate")),
                        db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return ProductService(state, db, ESTIMATE).settings(principal.tenant)


@router.put("/estimate/policy", response_model=EstimatePolicy, responses=_ERRORS, tags=["Estimate"],
            summary="Set your reserve policy and rounding increments")
def put_estimate_policy(policy: EstimatePolicy,
                        principal: Principal = Depends(require_scope("estimate")),
                        db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return _put_settings(state, db, principal, ESTIMATE, policy)


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
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key",
                                            description=_IDEMPOTENCY_DOC),
    principal: Principal = Depends(require_scope("verify")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    def build():
        subject = payload.subject
        scan_request = scan_request_from(payload.photos, payload.details, payload.model_version,
                                         subject.external_id)
        return (scan_request, {"subject": subject.model_dump(mode="json")},
                subject.external_id, subject.seller_id)

    return _create(state, db, principal, VERIFY, "POST /verify/checks", payload, idempotency_key, build)


@router.get("/verify/checks/{check_id}", response_model=VerifyCheckResponse,
            responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
            tags=["Verify"], summary="Retrieve a risk check")
def get_verify_check(check_id: str = Path(max_length=40),
                     principal: Principal = Depends(require_scope("verify")),
                     db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> JSONResponse:
    return _get(state, db, principal, VERIFY, check_id)


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
