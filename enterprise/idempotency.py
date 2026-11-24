"""
Idempotency keys for POST endpoints.

Semantics (per tenant and endpoint):
- First request with a key: executed; the 2xx response is stored with a hash of the request.
- Same key, same request within the TTL: the stored response is replayed
  (``Idempotent-Replayed: true``); nothing is re-executed or re-metered.
- Same key, different request: 422 ``idempotency_key_reused``.
- Errors (4xx/5xx) are not stored, so a corrected request can reuse the key.
- Two concurrent requests with one key: the unique constraint lets one win;
  the loser's transaction (including its valuation and metering rows) is
  rolled back and it replays the winner's response.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from enterprise.errors import ApiError
from enterprise.models import IdempotencyRecord

MAX_KEY_LENGTH = 255


def request_fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class IdempotentResult:
    status_code: int
    body: dict
    replayed: bool


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _lookup(
    session: Session, tenant_id: str, endpoint: str, key: str, now: datetime, ttl: timedelta
) -> Optional[IdempotencyRecord]:
    record = session.scalars(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.key == key,
        )
    ).first()
    if record is not None and _as_aware(record.created_at) < now - ttl:
        session.delete(record)
        session.flush()
        return None
    return record


def run_idempotent(
    session: Session,
    *,
    tenant_id: str,
    endpoint: str,
    key: Optional[str],
    payload: Any,
    handler: Callable[[], tuple[int, dict]],
    now: datetime,
    ttl: timedelta,
) -> IdempotentResult:
    """Run ``handler`` (which stages rows on ``session`` without committing) idempotently."""
    if key is None:
        status, body = handler()
        session.commit()
        return IdempotentResult(status, body, replayed=False)

    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ApiError(
            422,
            "invalid_idempotency_key",
            f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} characters.",
        )

    fingerprint = request_fingerprint(payload)
    existing = _lookup(session, tenant_id, endpoint, key, now, ttl)
    if existing is not None:
        if existing.request_hash != fingerprint:
            raise ApiError(
                422,
                "idempotency_key_reused",
                "This Idempotency-Key was already used with a different request.",
            )
        return IdempotentResult(existing.status_code, existing.response_body, replayed=True)

    status, body = handler()
    session.add(
        IdempotencyRecord(
            tenant_id=tenant_id,
            endpoint=endpoint,
            key=key,
            request_hash=fingerprint,
            status_code=status,
            response_body=body,
            created_at=now,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = _lookup(session, tenant_id, endpoint, key, now, ttl)
        if winner is not None and winner.request_hash == fingerprint:
            return IdempotentResult(winner.status_code, winner.response_body, replayed=True)
        raise ApiError(
            409, "idempotency_conflict", "A request with this Idempotency-Key is in progress."
        )
    return IdempotentResult(status, body, replayed=False)
