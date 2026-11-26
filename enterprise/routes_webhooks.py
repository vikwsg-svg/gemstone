"""HTTP routes for the account's webhook endpoint and its delivery log. Any valid key."""

from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from enterprise.auth import Principal, get_principal
from enterprise.deps import AppState, get_db, get_state
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.models import WebhookDelivery, WebhookEndpoint
from enterprise.schemas import (
    ErrorResponse,
    WebhookConfigRequest,
    WebhookConfigResponse,
    WebhookDeliveryList,
    WebhookDeliveryOut,
)
from enterprise.webhooks import (
    EVENTS,
    _aware,
    TEST_EVENT,
    check_url,
    delivery_body,
    dispatch_pending,
    emit,
    endpoint_for,
    new_secret,
)

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
_NOT_CONFIGURED = {404: {"model": ErrorResponse, "description": "No webhook endpoint is set."}}


def _config_body(endpoint: WebhookEndpoint, secret: Optional[str] = None) -> dict:
    return WebhookConfigResponse(id=endpoint.id, url=endpoint.url, events=endpoint.events,
                                 active=endpoint.active, created_at=_aware(endpoint.created_at),
                                 updated_at=_aware(endpoint.updated_at), secret=secret
                                 ).model_dump(mode="json")


def _endpoint(db: Session, principal: Principal) -> WebhookEndpoint:
    endpoint = endpoint_for(db, principal.tenant.id)
    if endpoint is None:
        raise ApiError(404, "webhook_not_configured", "No webhook endpoint is set for this account.")
    return endpoint


@router.get("/webhooks", response_model=WebhookConfigResponse,
            responses={**_ERRORS, **_NOT_CONFIGURED}, tags=["Webhooks"],
            summary="Your webhook endpoint")
def get_webhook(principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> JSONResponse:
    return JSONResponse(content=_config_body(_endpoint(db, principal)))


@router.put("/webhooks", response_model=WebhookConfigResponse, responses=_ERRORS,
            tags=["Webhooks"], summary="Set your webhook endpoint",
            description="Events: `intake.item.completed`, `verify.check.completed`, "
                        "`verify.check.flagged` (action is not `allow`) and `batch.completed`. "
                        "Deliveries are signed with `GemstoneAI-Signature: t=<unix>,v1=<hex>`, an "
                        "HMAC-SHA256 of `<t>.<raw body>` with your secret. The secret is returned "
                        "once, when the endpoint is created or `rotate_secret` is true.")
def put_webhook(payload: WebhookConfigRequest, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db),
                state: AppState = Depends(get_state)) -> JSONResponse:
    check_url(payload.url, allow_private=state.settings.webhook_allow_private_hosts)
    now = state.clock()
    events = sorted(set(payload.events)) if payload.events else list(EVENTS)
    endpoint = endpoint_for(db, principal.tenant.id)
    secret = None
    if endpoint is None:
        secret = new_secret()
        endpoint = WebhookEndpoint(id=new_id("whk"), tenant_id=principal.tenant.id,
                                   url=payload.url, events=events, secret=secret, active=True,
                                   created_at=now, updated_at=now)
        db.add(endpoint)
    else:
        endpoint.url, endpoint.events, endpoint.active, endpoint.updated_at = (
            payload.url, events, True, now)
        if payload.rotate_secret:
            secret = new_secret()
            endpoint.secret = secret
    db.commit()
    return JSONResponse(content=_config_body(endpoint, secret))


@router.delete("/webhooks", status_code=204, responses={**_ERRORS, **_NOT_CONFIGURED},
               tags=["Webhooks"], summary="Turn off your webhook endpoint")
def delete_webhook(principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> Response:
    endpoint = _endpoint(db, principal)
    endpoint.active = False
    endpoint.updated_at = state.clock()
    db.commit()
    return Response(status_code=204)


@router.post("/webhooks/test", response_model=WebhookDeliveryOut,
             responses={**_ERRORS, **_NOT_CONFIGURED}, tags=["Webhooks"],
             summary="Send a test event now and report what happened")
def test_webhook(principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                 state: AppState = Depends(get_state)) -> JSONResponse:
    endpoint = _endpoint(db, principal)
    if not endpoint.active:
        raise ApiError(409, "webhook_inactive", "The webhook endpoint is turned off.")
    event_id = emit(db, principal.tenant.id, TEST_EVENT,
                    {"message": "Test event from GemstoneAI Enterprise."}, state.clock())
    db.commit()
    dispatch_pending(state, principal.tenant.id)
    db.expire_all()
    return JSONResponse(content=WebhookDeliveryOut(
        **delivery_body(db.get(WebhookDelivery, event_id))).model_dump(mode="json"))


@router.get("/webhooks/deliveries", response_model=WebhookDeliveryList, responses=_ERRORS,
            tags=["Webhooks"], summary="Recent deliveries, newest first")
def list_deliveries(status: Optional[Literal["pending", "delivered", "failed"]] = None,
                    limit: int = Query(default=50, ge=1, le=200),
                    principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)) -> JSONResponse:
    query = select(WebhookDelivery).where(WebhookDelivery.tenant_id == principal.tenant.id)
    if status:
        query = query.where(WebhookDelivery.status == status)
    rows = db.scalars(query.order_by(WebhookDelivery.created_at.desc(),
                                     WebhookDelivery.id.desc()).limit(limit)).all()
    return JSONResponse(content=WebhookDeliveryList(
        data=[WebhookDeliveryOut(**delivery_body(r)) for r in rows]).model_dump(mode="json"))


@router.post("/webhooks/deliveries/{event_id}/retry", response_model=WebhookDeliveryOut,
             responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Not found."}},
             tags=["Webhooks"], summary="Send a failed event again")
def retry_delivery(background: BackgroundTasks, event_id: str = Path(max_length=40),
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                   state: AppState = Depends(get_state)) -> JSONResponse:
    delivery = db.scalars(select(WebhookDelivery).where(
        WebhookDelivery.id == event_id, WebhookDelivery.tenant_id == principal.tenant.id)).first()
    if delivery is None:
        raise ApiError(404, "delivery_not_found", "No delivery with this id exists.")
    if delivery.status != "delivered":
        delivery.status, delivery.attempts = "pending", 0
        db.commit()
        background.add_task(dispatch_pending, state, principal.tenant.id)
    return JSONResponse(content=WebhookDeliveryOut(**delivery_body(delivery)).model_dump(mode="json"))
