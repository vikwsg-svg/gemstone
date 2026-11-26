"""
Webhooks: tell a customer's system when something finished, instead of making it poll.

One endpoint per account (``PUT /webhooks``). Events:

    intake.item.completed   an Intake decision is ready
    verify.check.completed  a Verify check is ready
    verify.check.flagged    a Verify check's action is anything but "allow"
    batch.completed         a batch job finished (any product)

Delivery uses an outbox: the event row is written in the same transaction as
the record it reports, then dispatched after the response. Each dispatch run
tries every pending event once; failures stay pending and are retried on later
runs (after the next event, or ``python -m enterprise.cli webhooks-dispatch``
from cron) until ``webhook_max_attempts``, then they are ``failed`` and can be
retried by hand.

Every delivery is a JSON POST signed like this:

    GemstoneAI-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

Payloads carry a summary and the path of the full record, never photos.
Only https URLs on public hosts are accepted, and the host is checked again at
delivery time (DNS can change).
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from enterprise.deps import AppState
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.models import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

EVENTS = ("intake.item.completed", "verify.check.completed", "verify.check.flagged",
          "batch.completed")
TEST_EVENT = "webhook.test"
SIGNATURE_HEADER = "GemstoneAI-Signature"


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def check_url(url: str, *, allow_private: bool) -> None:
    """Refuse URLs that could reach internal services (SSRF)."""
    parts = urlsplit(url)
    if allow_private and parts.scheme in ("http", "https") and parts.hostname:
        return
    if parts.scheme != "https" or not parts.hostname:
        raise ApiError(422, "invalid_webhook_url", "The webhook URL must be an https URL.")
    if parts.username or parts.password:
        raise ApiError(422, "invalid_webhook_url", "The webhook URL must not contain credentials.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ApiError(422, "invalid_webhook_url", "The webhook host does not resolve.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise ApiError(422, "invalid_webhook_url",
                           "The webhook host must be a public internet address.")


def endpoint_for(session: Session, tenant_id: str) -> Optional[WebhookEndpoint]:
    return session.scalars(select(WebhookEndpoint).where(
        WebhookEndpoint.tenant_id == tenant_id)).first()


def emit(session: Session, tenant_id: str, event_type: str, data: dict,
         now: datetime) -> Optional[str]:
    """Queue an event if the account subscribed to it. Staged in the caller's transaction."""
    endpoint = endpoint_for(session, tenant_id)
    if endpoint is None or not endpoint.active:
        return None
    if event_type != TEST_EVENT and event_type not in endpoint.events:
        return None
    event_id = new_id("evt")
    session.add(WebhookDelivery(
        id=event_id, tenant_id=tenant_id, event_type=event_type, status="pending", attempts=0,
        payload={"id": event_id, "type": event_type, "created_at": now.isoformat(), "data": data},
        created_at=now,
    ))
    return event_id


def _deliver(state: AppState, client: httpx.Client, endpoint: WebhookEndpoint,
             delivery: WebhookDelivery) -> None:
    now = state.clock()
    delivery.attempts += 1
    delivery.last_attempt_at = now
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    try:
        check_url(endpoint.url, allow_private=state.settings.webhook_allow_private_hosts)
        response = client.post(endpoint.url, content=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "GemstoneAI-Webhooks/1",
            SIGNATURE_HEADER: sign(endpoint.secret, int(now.timestamp()), body),
            "GemstoneAI-Event-Id": delivery.id,
            "GemstoneAI-Event-Type": delivery.event_type,
        })
        delivery.last_status_code = response.status_code
        if 200 <= response.status_code < 300:
            delivery.status = "delivered"
            delivery.delivered_at = now
            delivery.last_error = None
            return
        delivery.last_error = f"HTTP {response.status_code}"
    except ApiError as exc:
        delivery.last_error = exc.message
    except httpx.HTTPError as exc:
        delivery.last_error = f"{type(exc).__name__}: could not reach the webhook URL"
    if delivery.attempts >= state.settings.webhook_max_attempts:
        delivery.status = "failed"


def dispatch_pending(state: AppState, tenant_id: Optional[str] = None, limit: int = 100) -> int:
    """Try each pending event once. Returns how many were delivered."""
    with state.db.session_scope() as session:
        query = select(WebhookDelivery).where(WebhookDelivery.status == "pending")
        if tenant_id is not None:
            query = query.where(WebhookDelivery.tenant_id == tenant_id)
        pending = session.scalars(query.order_by(WebhookDelivery.created_at).limit(limit)).all()
        if not pending:
            return 0
        delivered = 0
        with httpx.Client(timeout=state.settings.webhook_timeout_seconds, follow_redirects=False,
                          transport=state.webhook_transport) as client:
            for delivery in pending:
                endpoint = endpoint_for(session, delivery.tenant_id)
                if endpoint is None or not endpoint.active:
                    delivery.status = "failed"
                    delivery.last_error = "No active webhook endpoint."
                else:
                    _deliver(state, client, endpoint, delivery)
                    delivered += delivery.status == "delivered"
                session.commit()
        return delivered


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=timezone.utc)


def delivery_body(d: WebhookDelivery) -> dict:
    return {"id": d.id, "event_type": d.event_type, "status": d.status, "attempts": d.attempts,
            "last_status_code": d.last_status_code, "last_error": d.last_error,
            "created_at": _aware(d.created_at), "last_attempt_at": _aware(d.last_attempt_at),
            "delivered_at": _aware(d.delivered_at)}
