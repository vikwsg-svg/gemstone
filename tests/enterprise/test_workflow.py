"""Around each Intake and Verify record: queues, final calls, pilot metrics, batch, webhooks."""

import hashlib
import hmac
import json
import socket
from dataclasses import replace

import pytest

from enterprise.engine.base import PriceContext
from enterprise.errors import ApiError
from enterprise.webhooks import check_url
from tests.enterprise.helpers import BASE, RING, comp

ALL = ("scan", "intake", "verify")


@pytest.fixture
def key(make_tenant):
    return make_tenant(scopes=ALL)


def priced(low, high, **kw):
    return replace(RING, price_context=PriceContext(status="provided", low=low, high=high,
                                                    basis="retail replacement",
                                                    explanation="From comparables."), **kw)


def intake(client, key, i, **body):
    payload = {"photos": [{"url": f"https://photos.example.com/item-{i}.jpg"}],
               "reference": f"C-{i}", **body}
    r = client.post(f"{BASE}/intake/items", headers=key.headers, json=payload)
    assert r.status_code in (201, 202), r.text
    return r.json()


def verify(client, key, i, **subject):
    payload = {"photos": [{"url": f"https://photos.example.com/listing-{i}.jpg"}],
               "subject": {"type": "listing", "external_id": f"L-{i}", **subject}}
    r = client.post(f"{BASE}/verify/checks", headers=key.headers, json=payload)
    assert r.status_code in (201, 202), r.text
    return r.json()


# ── Intake queue, final calls and stats ──────────────────────────────────────


@pytest.fixture
def three_items(client, key, engine):
    engine.result = priced(300, 900)
    listed = intake(client, key, 1)
    engine.result = priced(4000, 6500)
    review = intake(client, key, 2)
    engine.result = priced(10, 40)
    declined = intake(client, key, 3)
    return listed, review, declined


def test_intake_queue_and_filters(client, key, three_items, clock):
    listed, review, declined = three_items
    queue = client.get(f"{BASE}/intake/items", headers=key.headers).json()
    assert queue["total"] == 3 and queue["has_more"] is False
    row = next(r for r in queue["data"] if r["id"] == listed["id"])
    assert row["decision"] == "list" and row["reason_codes"] == ["clear_to_list"]
    assert row["identified_as"].startswith("Platinum ring") and row["price_high"] == 900
    assert row["review_outcome"] is None

    only_list = client.get(f"{BASE}/intake/items?decision=list,decline", headers=key.headers).json()
    assert {r["id"] for r in only_list["data"]} == {listed["id"], declined["id"]}
    page = client.get(f"{BASE}/intake/items?limit=2", headers=key.headers).json()
    assert len(page["data"]) == 2 and page["has_more"] and page["next_offset"] == 2
    by_ref = client.get(f"{BASE}/intake/items?reference=C-2", headers=key.headers).json()
    assert [r["id"] for r in by_ref["data"]] == [review["id"]]
    assert client.get(f"{BASE}/intake/items?decision=sell", headers=key.headers).status_code == 422
    future = client.get(f"{BASE}/intake/items?created_from=2026-12-01", headers=key.headers).json()
    assert future["total"] == 0


def test_intake_final_call_is_recorded_with_history(client, key, three_items):
    listed, _, _ = three_items
    r = client.post(f"{BASE}/intake/items/{listed['id']}/review", headers=key.headers,
                    json={"outcome": "lab", "note": "Possible lab-grown center", "reviewer": "sam"})
    assert r.status_code == 200
    review = r.json()["review"]
    assert review["outcome"] == "lab" and review["agrees"] is False
    assert review["reviewer"] == "sam" and review["previous"] == []

    again = client.post(f"{BASE}/intake/items/{listed['id']}/review", headers=key.headers,
                        json={"outcome": "list"}).json()["review"]
    assert again["outcome"] == "list" and again["agrees"] is True
    assert [p["outcome"] for p in again["previous"]] == ["lab"]
    assert client.get(f"{BASE}/intake/items/{listed['id']}", headers=key.headers).json()["review"] == again

    awaiting = client.get(f"{BASE}/intake/items?reviewed=false", headers=key.headers).json()
    assert listed["id"] not in {r["id"] for r in awaiting["data"]}
    assert client.post(f"{BASE}/intake/items/{listed['id']}/review", headers=key.headers,
                       json={"outcome": "sell"}).status_code == 422


def test_intake_stats_measure_list_corrections(client, key, three_items):
    listed, review, _ = three_items
    client.post(f"{BASE}/intake/items/{listed['id']}/review", headers=key.headers,
                json={"outcome": "review"})
    client.post(f"{BASE}/intake/items/{review['id']}/review", headers=key.headers,
                json={"outcome": "review"})
    stats = client.get(f"{BASE}/intake/stats", headers=key.headers).json()
    assert stats["items"] == 3
    assert stats["by_decision"] == {"list": 1, "review": 1, "lab": 0, "decline": 1}
    assert stats["reviewed"] == 2 and stats["agreement_rate"] == 0.5
    assert stats["list_routed_reviewed"] == 1 and stats["list_routed_corrected"] == 1
    assert stats["list_correction_rate"] == 1.0
    assert client.get(f"{BASE}/intake/stats?start_date=2026-12-01&end_date=2026-11-01",
                      headers=key.headers).status_code == 422


def test_review_rules(client, key, make_tenant, engine, three_items):
    listed, _, _ = three_items
    other = make_tenant("Other", scopes=ALL)
    assert client.post(f"{BASE}/intake/items/{listed['id']}/review", headers=other.headers,
                       json={"outcome": "list"}).status_code == 404
    engine.pending = 100
    running = intake(client, key, 9)
    r = client.post(f"{BASE}/intake/items/{running['id']}/review", headers=key.headers,
                    json={"outcome": "list"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_completed"


def test_queue_csv_export(client, key, three_items):
    listed, _, _ = three_items
    client.post(f"{BASE}/intake/items/{listed['id']}/review", headers=key.headers,
                json={"outcome": "lab"})
    intake(client, key, 4, reference="=HYPERLINK(\"https://evil.example\")")
    r = client.get(f"{BASE}/intake/items?format=csv", headers=key.headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,created_at,status,reference,scan_id,identified_as")
    assert lines[0].endswith("decision,reason_codes,review_outcome,review_agrees,reviewed_at")
    assert len(lines) == 5
    assert any(",list,clear_to_list,lab,false," in line for line in lines)
    assert "'=HYPERLINK" in r.text and ",=HYPERLINK" not in r.text  # formulas neutralized


# ── Verify queue, verdicts and stats ─────────────────────────────────────────


def test_verify_queue_defaults_to_open_flags_by_risk(client, key):
    low = verify(client, key, 1, stated_value=900)       # price_too_low -> manual_review
    high = verify(client, key, 2, stated_value=30000)    # price_too_high -> manual_review
    clean = verify(client, key, 3, stated_value=5000)    # allow
    assert clean["risk"]["action"] == "allow"
    assert high["risk"]["action"] == "manual_review"

    queue = client.get(f"{BASE}/verify/checks", headers=key.headers).json()
    assert {r["id"] for r in queue["data"]} == {low["id"], high["id"]}
    assert all(r["action"] != "allow" for r in queue["data"])
    scores = [r["risk_score"] for r in queue["data"]]
    assert scores == sorted(scores, reverse=True)
    row = queue["data"][0]
    assert row["fired_checks"] and row["stated_value"] in (900, 30000)

    everything = client.get(f"{BASE}/verify/checks?action=&reviewed=any&order=newest",
                            headers=key.headers).json()
    assert everything["total"] == 3

    client.post(f"{BASE}/verify/checks/{low['id']}/review", headers=key.headers,
                json={"outcome": "confirmed"})
    open_flags = client.get(f"{BASE}/verify/checks", headers=key.headers).json()
    assert [r["id"] for r in open_flags["data"]] == [high["id"]]
    confirmed = client.get(f"{BASE}/verify/checks?reviewed=true&outcome=confirmed",
                           headers=key.headers).json()
    assert [r["id"] for r in confirmed["data"]] == [low["id"]]


def test_verify_stats_measure_flag_precision(client, key):
    flagged = [verify(client, key, i, stated_value=900) for i in range(3)]
    for i in range(3, 20):
        verify(client, key, i, stated_value=5000)
    client.post(f"{BASE}/verify/checks/{flagged[0]['id']}/review", headers=key.headers,
                json={"outcome": "confirmed"})
    client.post(f"{BASE}/verify/checks/{flagged[1]['id']}/review", headers=key.headers,
                json={"outcome": "cleared"})
    review = client.post(f"{BASE}/verify/checks/{flagged[1]['id']}/review", headers=key.headers,
                         json={"outcome": "cleared"}).json()["review"]
    assert review["agrees"] is False  # flagged, but cleared

    stats = client.get(f"{BASE}/verify/stats", headers=key.headers).json()
    assert stats["checks"] == 20 and stats["flagged"] == 3
    assert stats["by_action"]["manual_review"] == 3 and stats["by_action"]["allow"] == 17
    assert stats["flagged_reviewed"] == 2 and stats["confirmed"] == 1 and stats["cleared"] == 1
    assert stats["flag_precision"] == 0.5
    assert stats["top_flags"]["count"] == 1  # 5% of 20
    assert stats["confirmed_stated_value"] == {"USD": 900.0}


# ── Batch ────────────────────────────────────────────────────────────────────


def _upload(client, key, path, csv_text):
    return client.post(f"{BASE}/{path}", headers=key.headers,
                       files={"file": ("rows.csv", csv_text.encode(), "text/csv")})


def test_intake_batch(client, key, engine):
    engine.result = priced(300, 900)
    csv_text = ("row_id,photo_urls,consignor_description,metal_type\n"
                "A1,https://photos.example.com/a1.jpg,Grandmother's ring,platinum\n"
                "A2,,No photo,\n")
    r = _upload(client, key, "intake/items/batch", csv_text)
    assert r.status_code == 202 and r.json()["product"] == "intake"
    job = client.get(f"{BASE}/intake/items/batch/{r.json()['job_id']}", headers=key.headers).json()
    assert job["status"] == "completed" and job["processed_rows"] == 2 and job["failed_rows"] == 1
    assert job["results_url"].endswith(f"/intake/items/batch/{job['job_id']}/results")

    results = client.get(f"{BASE}/intake/items/batch/{job['job_id']}/results", headers=key.headers)
    lines = results.text.strip().splitlines()
    assert lines[0].startswith("row_number,row_id,row_status,error,id,")
    assert ",A1,completed,," in lines[1] and ",list,clear_to_list," in lines[1]
    assert lines[2].startswith("2,A2,error,")

    queue = client.get(f"{BASE}/intake/items", headers=key.headers).json()
    assert [r["reference"] for r in queue["data"]] == ["A1"]  # row_id is the reference
    _, details, _ = engine.analyze_calls[0]
    assert "Grandmother's ring" in details["notes"] and details["metal"]["type"] == "platinum"
    usage = client.get(f"{BASE}/usage", headers=key.headers).json()["data"]
    assert [(u["product"], u["scans_total"]) for u in usage] == [("intake", 1)]
    # A job is only visible under its own product.
    assert client.get(f"{BASE}/scans/batch/{job['job_id']}", headers=key.headers).status_code == 404
    assert client.get(f"{BASE}/verify/checks/batch/{job['job_id']}", headers=key.headers).status_code == 404


def test_verify_batch(client, key):
    csv_text = ("external_id,photo_urls,stated_value,seller_id,subject_type\n"
                "L-1,https://photos.example.com/l1.jpg,900,s-1,claim\n"
                "L-2,https://photos.example.com/l2.jpg,5000,s-2,\n")
    r = _upload(client, key, "verify/checks/batch", csv_text)
    job_id = r.json()["job_id"]
    results = client.get(f"{BASE}/verify/checks/batch/{job_id}/results", headers=key.headers).text
    lines = results.strip().splitlines()
    header = lines[0].split(",")
    rows = [dict(zip(header, line.split(","))) for line in lines[1:]]
    assert [r["action"] for r in rows] == ["manual_review", "allow"]
    assert rows[0]["subject_type"] == "claim" and rows[1]["subject_type"] == "listing"
    assert rows[0]["fired_checks"] == "price_too_low" and rows[0]["seller_id"] == "s-1"


def test_product_batch_needs_the_product_scope(client, make_tenant):
    scan_only = make_tenant(scopes=("scan",))
    r = _upload(client, scan_only, "verify/checks/batch", "photo_urls\nhttps://p.example.com/x.jpg\n")
    assert r.status_code == 403


# ── Webhooks ─────────────────────────────────────────────────────────────────


@pytest.fixture
def public_dns(monkeypatch):
    def fake(host, port, *args, **kwargs):
        address = {"hooks.example.com": "93.184.216.34", "internal.example.com": "10.0.0.7",
                   "loopback.example.com": "127.0.0.1"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr("enterprise.webhooks.socket.getaddrinfo", fake)


@pytest.fixture
def webhook(client, key, public_dns):
    r = client.put(f"{BASE}/webhooks", headers=key.headers,
                   json={"url": "https://hooks.example.com/gemstone"})
    assert r.status_code == 200, r.text
    return r.json()


def _verify_signature(request, secret):
    header = dict(part.split("=", 1) for part in request.headers["GemstoneAI-Signature"].split(","))
    expected = hmac.new(secret.encode(), f"{header['t']}.".encode() + request.content,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header["v1"])


@pytest.mark.parametrize("url,ok", [
    ("https://hooks.example.com/x", True),
    ("http://hooks.example.com/x", False),
    ("https://internal.example.com/x", False),
    ("https://loopback.example.com/x", False),
    ("https://user:pw@hooks.example.com/x", False),
])
def test_webhook_urls_must_be_public_https(public_dns, url, ok):
    if ok:
        check_url(url, allow_private=False)
    else:
        with pytest.raises(ApiError):
            check_url(url, allow_private=False)


def test_webhook_secret_is_shown_once(client, key, webhook):
    assert webhook["secret"].startswith("whsec_") and webhook["active"] is True
    assert set(webhook["events"]) == {"intake.item.completed", "verify.check.completed",
                                      "verify.check.flagged", "batch.completed"}
    assert client.get(f"{BASE}/webhooks", headers=key.headers).json()["secret"] is None
    same = client.put(f"{BASE}/webhooks", headers=key.headers,
                      json={"url": "https://hooks.example.com/v2"}).json()
    assert same["secret"] is None and same["url"].endswith("/v2")
    rotated = client.put(f"{BASE}/webhooks", headers=key.headers,
                         json={"url": "https://hooks.example.com/v2", "rotate_secret": True}).json()
    assert rotated["secret"] and rotated["secret"] != webhook["secret"]
    bad = client.put(f"{BASE}/webhooks", headers=key.headers,
                     json={"url": "https://internal.example.com/x"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_webhook_url"


def test_completed_records_are_delivered_signed(client, key, webhook, hooks):
    item = intake(client, key, 1)
    assert hooks.events() == ["intake.item.completed"]
    request = hooks.requests[0]
    assert _verify_signature(request, webhook["secret"])
    event = json.loads(request.content)
    assert event["type"] == "intake.item.completed" and event["id"] == request.headers["GemstoneAI-Event-Id"]
    assert event["data"]["id"] == item["id"] and event["data"]["path"] == f"/intake/items/{item['id']}"
    assert event["data"]["summary"]["decision"] == item["routing"]["decision"]
    assert "photos" not in request.content.decode()

    verify(client, key, 2, stated_value=900)
    verify(client, key, 3, stated_value=5000)
    assert hooks.events()[1:] == ["verify.check.completed", "verify.check.flagged",
                                  "verify.check.completed"]


def test_webhook_events_filter_and_turn_off(client, key, webhook, hooks):
    client.put(f"{BASE}/webhooks", headers=key.headers,
               json={"url": "https://hooks.example.com/gemstone", "events": ["verify.check.flagged"]})
    intake(client, key, 1)
    verify(client, key, 2, stated_value=5000)
    verify(client, key, 3, stated_value=900)
    assert hooks.events() == ["verify.check.flagged"]
    assert client.delete(f"{BASE}/webhooks", headers=key.headers).status_code == 204
    verify(client, key, 4, stated_value=900)
    assert len(hooks.requests) == 1


def test_failed_deliveries_retry_then_fail(client, key, webhook, hooks, settings):
    hooks.status = 500
    intake(client, key, 1)
    deliveries = client.get(f"{BASE}/webhooks/deliveries", headers=key.headers).json()["data"]
    assert deliveries[0]["status"] == "pending" and deliveries[0]["attempts"] == 1
    assert deliveries[0]["last_status_code"] == 500
    for i in range(2, 2 + settings.webhook_max_attempts):  # each request retries pending events
        intake(client, key, i)
    first = client.get(f"{BASE}/webhooks/deliveries?status=failed", headers=key.headers).json()["data"]
    assert deliveries[0]["id"] in {d["id"] for d in first}

    hooks.status = 200
    r = client.post(f"{BASE}/webhooks/deliveries/{deliveries[0]['id']}/retry", headers=key.headers)
    assert r.status_code == 200
    after = client.get(f"{BASE}/webhooks/deliveries?status=delivered", headers=key.headers).json()["data"]
    assert deliveries[0]["id"] in {d["id"] for d in after}


def test_webhook_test_event_and_isolation(client, key, make_tenant, webhook, hooks):
    r = client.post(f"{BASE}/webhooks/test", headers=key.headers).json()
    assert r["event_type"] == "webhook.test" and r["status"] == "delivered"
    assert r["created_at"].endswith("Z") or r["created_at"].endswith("+00:00")
    other = make_tenant("Other", scopes=ALL)
    assert client.get(f"{BASE}/webhooks/deliveries", headers=other.headers).json()["data"] == []
    assert client.get(f"{BASE}/webhooks", headers=other.headers).status_code == 404
    assert client.post(f"{BASE}/webhooks/deliveries/{r['id']}/retry",
                       headers=other.headers).status_code == 404


def test_batch_completion_is_announced(client, key, webhook, hooks):
    csv_text = "row_id,photo_urls\nA1,https://photos.example.com/a1.jpg\n"
    _upload(client, key, "intake/items/batch", csv_text)
    assert hooks.events() == ["intake.item.completed", "batch.completed"]
    batch_event = json.loads(hooks.requests[1].content)
    assert batch_event["data"]["product"] == "intake" and batch_event["data"]["processed_rows"] == 1


# ── Account ──────────────────────────────────────────────────────────────────


def test_account_endpoint(client, make_tenant):
    sandbox = make_tenant("Demo Resale", environment="sandbox", scopes=("intake", "verify"))
    body = client.get(f"{BASE}/account", headers=sandbox.headers).json()
    assert body == {"object": "account", "tenant_id": sandbox.tenant_id, "name": "Demo Resale",
                    "environment": "sandbox", "scopes": ["intake", "verify"],
                    "pinned_model_version": None, "key_id": sandbox.key_id}
    assert client.get(f"{BASE}/account").status_code == 401
