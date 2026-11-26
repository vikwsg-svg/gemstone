"""The Scan API: today's scan result plus comparables, in one response, with the business rules."""

import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from enterprise.app import create_app
from enterprise.engine.base import AnalysisResult, PriceContext
from enterprise.models import Scan
from tests.enterprise.helpers import BASE, RING, comp, photo_urls, scan_body


def _scan(client, tenant, **overrides):
    return client.post(f"{BASE}/scans", headers=tenant.headers, json=scan_body(**overrides))


def test_one_call_returns_identification_price_and_comparables(client, tenant, engine, db):
    resp = _scan(client, tenant, photos=photo_urls(2), client_reference="POLICY-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["object"] == "scan" and body["product"] == "scan"
    assert body["status"] == "completed"
    assert body["model_version"] == "scan-v7"
    assert body["synthetic_data"] is False
    assert body["client_reference"] == "POLICY-1"

    ident = body["identification"]
    assert ident == {
        "status": "identified", "summary": RING.summary, "confidence": 0.9,
        "observations": list(RING.observations),
        "alternatives": [{"label": "Center stone is moissanite", "probability": 0.05}],
        "open_questions": list(RING.open_questions), "next_evidence": list(RING.next_evidence),
    }
    assert body["price_context"] == {
        "status": "provided", "currency": "USD", "low": 4000, "high": 6501,
        "basis": "retail replacement", "explanation": "From 8 comparable rings.",
        "withheld_reason": None,
    }
    comps = body["comparables"]
    assert comps["status"] == "found" and comps["total"] == 8
    assert comps["asking"] == 8 and comps["realized"] == 0
    first = comps["items"][0]
    assert first["price_type"] == "asking" and first["url"] == "https://market.example/0"
    assert first["differences"] == ["Smaller center stone"]

    assert engine.comparables_calls == ["ana_ring"]
    with db.session_scope() as s:
        row = s.scalars(select(Scan)).one()
        assert row.status == "completed" and row.priced is True and row.photo_count == 2
        assert row.analysis_ref == "ana_ring" and row.model_version == "scan-v7"


def test_details_are_passed_to_the_scan_as_hints(client, tenant, engine):
    details = {"item_type": "ring", "metal": {"type": "platinum", "weight_grams": 6.4},
               "brand": "Maison Aurelle", "signed": True, "notes": "From a 2019 appraisal"}
    body = _scan(client, tenant, details=details).json()
    _, hints, _ = engine.analyze_calls[-1]
    assert hints == details
    assert body["inputs"]["details"]["metal"]["weight_grams"] == 6.4


def test_inline_photo_is_sent_to_the_scan_but_stored_as_metadata(client, tenant, engine, db):
    data = base64.b64encode(b"\xff\xd8" + b"jpeg-bytes" * 300).decode()
    body = _scan(client, tenant, photos=[{"data": data, "media_type": "image/jpeg"}]).json()
    stored = body["inputs"]["photos"][0]
    assert stored["bytes"] == 3002 and stored["media_type"] == "image/jpeg" and "data" not in stored
    sent = engine.analyze_calls[-1][0][0]
    assert sent.data is not None and sent.bytes == 3002
    with db.session_scope() as s:
        assert "jpeg-bytes" not in str(s.scalars(select(Scan)).one().request_payload)


def test_fewer_than_five_comparables_withholds_the_price(client, tenant, engine):
    engine.comps = [comp(i, price_type="realized") for i in range(4)]
    body = _scan(client, tenant).json()
    price = body["price_context"]
    assert price["status"] == "withheld"
    assert price["low"] is None and price["high"] is None and price["basis"] is None
    assert "Fewer than 5 comparables" in price["withheld_reason"]
    assert body["comparables"]["total"] == 4 and body["comparables"]["realized"] == 4
    usage = client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"][0]
    assert (usage["scans_total"], usage["scans_priced"]) == (1, 0)


def test_price_withheld_by_the_scan_is_passed_through(client, tenant, engine):
    engine.result = replace(RING, price_context=PriceContext(
        status="withheld", withheld_reason="Treatment drives the value; a lab report is needed."))
    body = _scan(client, tenant).json()
    assert body["price_context"]["status"] == "withheld"
    assert body["price_context"]["withheld_reason"].startswith("Treatment drives")
    assert body["comparables"]["total"] == 8  # the evidence is still returned


def test_unidentified_item_has_no_price_and_no_comparables(client, tenant, engine):
    engine.result = AnalysisResult(analysis_id="ana_x", model_version="scan-v7", identified=False,
                                   summary="Too blurry to identify", confidence=0.0,
                                   next_evidence=("Clear, well-lit photos",))
    body = _scan(client, tenant).json()
    assert body["identification"]["status"] == "unidentified"
    assert body["price_context"]["status"] == "withheld"
    assert body["price_context"]["withheld_reason"] == "The item could not be identified."
    assert body["comparables"] == {"status": "none_found", "total": 0, "realized": 0,
                                   "asking": 0, "items": []}
    assert engine.comparables_calls == []


def test_no_comparables_found(client, tenant, engine):
    engine.comps = []
    body = _scan(client, tenant).json()
    assert body["comparables"]["status"] == "none_found"
    assert body["price_context"]["status"] == "withheld"


def test_processing_scan_completes_on_retrieval(client, tenant, engine):
    engine.pending = 9  # POST polls 8 times (wait 8 s / 1 s), so it is still running
    resp = _scan(client, tenant)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "processing" and body["model_version"] is None
    assert body["identification"]["status"] == "processing"
    assert body["price_context"]["status"] == "processing"
    assert body["comparables"]["status"] == "processing"
    assert f"/scans/{body['scan_id']}" in body["warnings"][0]
    assert client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"] == []  # not metered

    done = client.get(f"{BASE}/scans/{body['scan_id']}", headers=tenant.headers).json()
    assert done["status"] == "completed" and done["scan_id"] == body["scan_id"]
    assert done["identification"]["summary"] == RING.summary
    assert done["price_context"]["status"] == "provided"
    again = client.get(f"{BASE}/scans/{body['scan_id']}", headers=tenant.headers).json()
    assert again == done
    assert client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"][0]["scans_total"] == 1


def test_scan_outage_is_503_and_stores_nothing(client, tenant, engine, db):
    engine.fail = True
    resp = _scan(client, tenant)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "scan_unavailable"
    engine.fail, engine.fail_comparables = False, True
    assert _scan(client, tenant).status_code == 503
    with db.session_scope() as s:
        assert s.scalars(select(Scan)).all() == []


def test_live_tenant_without_scan_backend_is_503(db, settings, clock, make_tenant):
    app = create_app(settings, db=db, live_engine=None, clock=clock)
    tenant = make_tenant()
    with TestClient(app) as c:
        resp = c.post(f"{BASE}/scans", headers=tenant.headers, json=scan_body())
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "scan_unavailable"


def test_model_version_pin_is_passed_through(client, make_tenant, engine):
    pinned = make_tenant(pin="scan-v6")
    body = _scan(client, pinned).json()
    assert engine.analyze_calls[-1][2] == "scan-v6" and body["model_version"] == "scan-v6"
    override = _scan(client, pinned, model_version="scan-v7").json()
    assert engine.analyze_calls[-1][2] == "scan-v7" and override["model_version"] == "scan-v7"


def test_unknown_model_version_is_422(client, tenant, make_tenant):
    resp = _scan(client, tenant, model_version="scan-v1")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "unknown_model_version"
    assert err["details"] == {"requested": "scan-v1", "available": ["scan-v6", "scan-v7"]}
    stale = make_tenant(pin="scan-v1")
    assert "account pin" in _scan(client, stale).json()["error"]["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"photos": []},
        {"photos": photo_urls(6)},
        {"photos": [{"url": "https://x.example/a.jpg", "data": "aGk=", "media_type": "image/jpeg"}]},
        {"photos": [{"data": "not base64!!", "media_type": "image/jpeg"}]},
        {"photos": [{"data": "aGk="}]},
        {"photos": [{"url": "http://insecure.example/a.jpg"}]},
        {"details": {"item_type": "watch"}},
        {"details": {"colour": "blue"}},
        {"unexpected": True},
    ],
)
def test_invalid_requests_are_rejected(client, tenant, overrides):
    payload = scan_body()
    payload.update(overrides)
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=payload)
    assert resp.status_code == 422, overrides
    assert resp.json()["error"]["code"] == "invalid_request"


def test_sandbox_returns_example_results_for_every_outcome(client, make_tenant):
    sandbox = make_tenant(environment="sandbox")
    assert sandbox.key.startswith("gsk_test_")

    def scan(**kw):
        return client.post(f"{BASE}/scans", headers=sandbox.headers, json=scan_body(**kw)).json()

    ring = scan(details={"item_type": "ring"})
    assert ring["synthetic_data"] is True and "example" in ring["warnings"][0]
    assert ring["model_version"] == "sandbox-2026.11"
    assert ring["price_context"]["status"] == "provided" and ring["comparables"]["total"] >= 5

    ruby = scan(details={"item_type": "loose_stone"})
    assert ruby["price_context"]["status"] == "withheld"  # only 3 comparables
    assert ruby["comparables"]["total"] == 3

    tiny = scan(photos=[{"data": base64.b64encode(b"tiny").decode(), "media_type": "image/png"}])
    assert tiny["identification"]["status"] == "unidentified"

    photo = [{"data": base64.b64encode(b"\xff\xd8" + b"p" * 4000).decode(), "media_type": "image/jpeg"}]
    assert scan(photos=photo)["identification"] == scan(photos=photo)["identification"]

    bad_pin = client.post(f"{BASE}/scans", headers=sandbox.headers,
                          json=scan_body(model_version="scan-v7"))
    assert bad_pin.status_code == 422
