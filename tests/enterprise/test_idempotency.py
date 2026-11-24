"""Idempotency keys on POST /valuations (and batch)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from enterprise.errors import ApiError
from enterprise.idempotency import request_fingerprint, run_idempotent
from enterprise.models import IdempotencyRecord, UsageDaily, Valuation
from tests.enterprise.helpers import BASE, FIXED_NOW, stone


def _post(client, tenant, key=None, **overrides):
    headers = dict(tenant.headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(f"{BASE}/valuations", headers=headers, json=stone(**overrides))


def _count(db, model):
    with db.session_scope() as s:
        return s.scalar(select(func.count()).select_from(model))


def _usage_total(db):
    with db.session_scope() as s:
        return s.scalar(select(func.coalesce(func.sum(UsageDaily.valuations_total), 0)))


def test_replay_returns_same_valuation_and_meters_once(client, tenant, db, live_engine):
    first = _post(client, tenant, key="order-123")
    second = _post(client, tenant, key="order-123")

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("idempotent-replayed") == "true"
    assert "idempotent-replayed" not in first.headers
    assert _count(db, Valuation) == 1
    assert _usage_total(db) == 1
    assert len(live_engine.calls) == 1  # not re-executed


def test_same_key_different_body_is_rejected(client, tenant, db):
    _post(client, tenant, key="k1")
    resp = _post(client, tenant, key="k1", carat=3.0)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "idempotency_key_reused"
    assert _count(db, Valuation) == 1


def test_keys_are_scoped_per_tenant(client, make_tenant, db):
    a, b = make_tenant("A"), make_tenant("B")
    ra = _post(client, a, key="shared-key")
    rb = _post(client, b, key="shared-key")
    assert ra.json()["valuation_id"] != rb.json()["valuation_id"]
    assert _count(db, Valuation) == 2


def test_without_key_each_post_creates_a_valuation(client, tenant, db):
    _post(client, tenant)
    _post(client, tenant)
    assert _count(db, Valuation) == 2
    assert _count(db, IdempotencyRecord) == 0


def test_errors_are_not_stored_so_key_can_be_reused(client, tenant, db):
    bad = _post(client, tenant, key="k2", model_version="does-not-exist")
    assert bad.status_code == 422
    good = _post(client, tenant, key="k2")
    assert good.status_code == 201
    assert _count(db, Valuation) == 1


def test_key_expires_after_ttl(client, tenant, db, clock):
    first = _post(client, tenant, key="k3").json()
    clock.advance(hours=25)
    second = _post(client, tenant, key="k3").json()
    assert first["valuation_id"] != second["valuation_id"]
    assert _count(db, Valuation) == 2


def test_overlong_key_is_rejected(client, tenant):
    resp = _post(client, tenant, key="x" * 256)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_idempotency_key"


def test_concurrent_duplicate_replays_the_winner(db, tenant):
    """Simulate a race: another request commits the same key between lookup and commit."""
    payload = {"a": 1}
    winner_body = {"valuation_id": "val_winner"}

    with db.session_scope() as session:

        def handler():
            # The competing request wins the race and commits first.
            with db.session_scope() as other:
                other.add(
                    IdempotencyRecord(
                        tenant_id=tenant.tenant_id, endpoint="POST /valuations", key="race",
                        request_hash=request_fingerprint(payload), status_code=201,
                        response_body=winner_body, created_at=FIXED_NOW,
                    )
                )
                other.commit()
            return 201, {"valuation_id": "val_loser"}

        result = run_idempotent(
            session, tenant_id=tenant.tenant_id, endpoint="POST /valuations", key="race",
            payload=payload, handler=handler, now=FIXED_NOW, ttl=timedelta(hours=24),
        )
    assert result.replayed is True
    assert result.body == winner_body


def test_concurrent_duplicate_with_different_body_conflicts(db, tenant):
    with db.session_scope() as session:

        def handler():
            with db.session_scope() as other:
                other.add(
                    IdempotencyRecord(
                        tenant_id=tenant.tenant_id, endpoint="POST /valuations", key="race2",
                        request_hash="different", status_code=201, response_body={},
                        created_at=FIXED_NOW,
                    )
                )
                other.commit()
            return 201, {}

        with pytest.raises(ApiError) as exc:
            run_idempotent(
                session, tenant_id=tenant.tenant_id, endpoint="POST /valuations", key="race2",
                payload={"a": 1}, handler=handler, now=FIXED_NOW, ttl=timedelta(hours=24),
            )
    assert exc.value.status_code == 409
