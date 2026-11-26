"""Tenant isolation, API key auth and product scopes."""

from datetime import datetime, timezone

from enterprise.models import ApiKey, Tenant
from tests.enterprise.helpers import BASE, scan_body


def _create(client, tenant):
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=scan_body())
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_owner_can_retrieve_scan(client, tenant):
    created = _create(client, tenant)
    resp = client.get(f"{BASE}/scans/{created['scan_id']}", headers=tenant.headers)
    assert resp.status_code == 200
    assert resp.json() == created


def test_other_tenant_cannot_see_scan(client, make_tenant):
    a = make_tenant("Auction House A")
    b = make_tenant("Insurer B")
    created = _create(client, a)
    resp = client.get(f"{BASE}/scans/{created['scan_id']}", headers=b.headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "scan_not_found"


def test_other_tenant_cannot_see_batch_job(client, make_tenant):
    a = make_tenant("A")
    b = make_tenant("B")
    csv_text = "photo_urls\nhttps://p.example/a.jpg\n"
    job = client.post(
        f"{BASE}/scans/batch",
        headers=a.headers,
        files={"file": ("s.csv", csv_text, "text/csv")},
    ).json()
    for path in (f"/scans/batch/{job['job_id']}", f"/scans/batch/{job['job_id']}/results"):
        resp = client.get(f"{BASE}{path}", headers=b.headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "batch_job_not_found"


def test_usage_is_per_tenant(client, make_tenant):
    a = make_tenant("A")
    b = make_tenant("B")
    _create(client, a)
    _create(client, a)
    _create(client, b)
    usage_a = client.get(f"{BASE}/usage", headers=a.headers).json()
    usage_b = client.get(f"{BASE}/usage", headers=b.headers).json()
    assert usage_a["data"][0]["scans_total"] == 2
    assert usage_b["data"][0]["scans_total"] == 1


def test_missing_key_is_401(client):
    resp = client.post(f"{BASE}/scans", json=scan_body())
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"
    assert resp.headers["www-authenticate"] == "Bearer"


def test_malformed_and_wrong_keys_are_401(client, tenant):
    for key in ("nope", tenant.key[:-1] + ("A" if tenant.key[-1] != "A" else "B")):
        resp = client.post(
            f"{BASE}/scans", headers={"Authorization": f"Bearer {key}"}, json=scan_body()
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"


def test_x_api_key_header_is_accepted(client, tenant):
    resp = client.post(f"{BASE}/scans", headers={"X-API-Key": tenant.key}, json=scan_body())
    assert resp.status_code == 201


def test_revoked_key_is_401(client, tenant, db):
    with db.session_scope() as s:
        s.get(ApiKey, tenant.key_id).revoked_at = datetime.now(timezone.utc)
        s.commit()
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=scan_body())
    assert resp.status_code == 401


def test_suspended_tenant_is_403(client, tenant, db):
    with db.session_scope() as s:
        s.get(Tenant, tenant.tenant_id).status = "suspended"
        s.commit()
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=scan_body())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "tenant_inactive"


def test_key_without_scan_scope_is_403(client, make_tenant):
    verify_only = make_tenant(scopes=("verify",))
    resp = client.post(f"{BASE}/scans", headers=verify_only.headers, json=scan_body())
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "insufficient_scope"
    assert err["details"] == {"required_scope": "scan", "key_scopes": ["verify"]}
    # Usage is a platform endpoint and only needs a valid key.
    assert client.get(f"{BASE}/usage", headers=verify_only.headers).status_code == 200


def test_sandbox_tenant_gets_example_results(client, make_tenant, engine):
    sandbox = make_tenant(environment="sandbox")
    assert sandbox.key.startswith("gsk_test_")
    body = client.post(f"{BASE}/scans", headers=sandbox.headers, json=scan_body()).json()
    assert body["synthetic_data"] is True
    assert any("example" in w for w in body["warnings"])
    assert engine.analyze_calls == []  # the live scan is never touched by sandbox keys


def test_live_tenant_without_engine_gets_503(db, settings, clock, make_tenant):
    from fastapi.testclient import TestClient

    from enterprise.app import create_app

    app = create_app(settings, db=db, live_engine=None, clock=clock)
    live = make_tenant()
    with TestClient(app) as c:
        resp = c.post(f"{BASE}/scans", headers=live.headers, json=scan_body())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "scan_unavailable"
    assert live.key.startswith("gsk_live_")


def test_usage_date_range_validation(client, tenant):
    resp = client.get(
        f"{BASE}/usage",
        headers=tenant.headers,
        params={"start_date": "2026-11-20", "end_date": "2026-11-01"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_date_range"
