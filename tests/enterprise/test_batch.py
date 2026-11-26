"""CSV batch scans: submission, async processing, status and results CSV."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from enterprise.app import create_app
from enterprise.models import BatchJob, Scan
from tests.enterprise.helpers import BASE, RING

CSV_OK = """row_id,photo_urls,item_type,metal_type,metal_weight_grams,notes
A-1,https://p.example/a1.jpg|https://p.example/a1-band.jpg,ring,platinum,6.4,
A-2,https://p.example/a2.jpg,,,,from catalogue
A-3,http://insecure.example/a3.jpg,,,,
=HYPERLINK("x"),https://p.example/a4.jpg,,,,
"""


def _submit(client, tenant, text=CSV_OK, headers=None, **data):
    return client.post(
        f"{BASE}/scans/batch",
        headers={**tenant.headers, **(headers or {})},
        files={"file": ("items.csv", text.encode("utf-8"), "text/csv")},
        data=data,
    )


def _results(client, tenant, job_id):
    resp = client.get(f"{BASE}/scans/batch/{job_id}/results", headers=tenant.headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(resp.text)))


def test_batch_round_trip(client, tenant, engine, db):
    resp = _submit(client, tenant)
    assert resp.status_code == 202
    job = resp.json()
    assert job["object"] == "batch_job" and job["total_rows"] == 4

    status = client.get(f"{BASE}/scans/batch/{job['job_id']}", headers=tenant.headers).json()
    assert status["status"] == "completed"
    assert (status["processed_rows"], status["priced_rows"], status["failed_rows"]) == (4, 3, 1)
    assert status["results_url"] == f"{BASE}/scans/batch/{job['job_id']}/results"

    rows = _results(client, tenant, job["job_id"])
    assert [r["row_id"] for r in rows] == ["A-1", "A-2", "A-3", "'=HYPERLINK(\"x\")"]
    assert rows[0]["status"] == "completed" and rows[0]["price_status"] == "provided"
    assert rows[0]["identified_as"] == RING.summary
    assert rows[0]["identification_confidence"] == "0.9"
    assert (rows[0]["price_low"], rows[0]["price_high"]) == ("4000", "6501")
    assert rows[0]["comparables"] == "8" and rows[0]["scan_id"].startswith("scn_")
    assert rows[2]["status"] == "error" and "https" in rows[2]["error"]

    photos, hints, _ = engine.analyze_calls[0]
    assert len(photos) == 2
    assert hints == {"item_type": "ring", "metal": {"type": "platinum", "weight_grams": 6.4}}
    assert engine.analyze_calls[1][1] == {"notes": "from catalogue"}

    scan = client.get(f"{BASE}/scans/{rows[0]['scan_id']}", headers=tenant.headers).json()
    assert scan["batch_job_id"] == job["job_id"] and scan["client_reference"] == "A-1"
    with db.session_scope() as s:
        linked = s.scalars(select(Scan).where(Scan.batch_job_id == job["job_id"])).all()
        assert sorted(v.batch_row for v in linked) == [1, 2, 4]
    assert client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"][0]["scans_total"] == 3


def test_batch_rows_with_withheld_prices(client, tenant, engine):
    engine.comps = []
    job = _submit(client, tenant, "photo_urls\nhttps://p.example/x.jpg\n").json()
    status = client.get(f"{BASE}/scans/batch/{job['job_id']}", headers=tenant.headers).json()
    assert status["unpriced_rows"] == 1
    row = _results(client, tenant, job["job_id"])[0]
    assert row["price_status"] == "withheld" and row["price_low"] == ""
    assert "Fewer than 5" in row["withheld_reason"]


def test_batch_model_version_is_passed_through(client, tenant, engine):
    job = _submit(client, tenant, "photo_urls\nhttps://p.example/x.jpg\n",
                  model_version="scan-v6").json()
    assert job["model_version"] == "scan-v6"
    assert engine.analyze_calls[-1][2] == "scan-v6"


@pytest.mark.parametrize("text", ["variety,carat\nruby,1\n", "", "photo_urls\n"])
def test_invalid_csv_is_rejected_up_front(client, tenant, text):
    resp = _submit(client, tenant, text)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_csv"


def test_non_utf8_is_rejected(client, tenant):
    resp = client.post(f"{BASE}/scans/batch", headers=tenant.headers,
                       files={"file": ("s.csv", "photo_urls\nhttps://p/é.jpg\n".encode("latin-1"))})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_csv"


def test_row_and_size_limits(db, settings, engine, clock, make_tenant):
    small = settings.model_copy(update={"batch_max_rows": 2, "batch_max_bytes": 200})
    tenant = make_tenant()
    with TestClient(create_app(small, db=db, live_engine=engine, clock=clock)) as c:
        too_many = _submit(c, tenant, "photo_urls\n" + "https://p.example/x.jpg\n" * 3)
        assert too_many.status_code == 422 and too_many.json()["error"]["code"] == "too_many_rows"
        too_big = _submit(c, tenant, "photo_urls\nhttps://p.example/" + "x" * 300 + ".jpg\n")
        assert too_big.status_code == 413


def test_results_before_completion_is_409(client, tenant, db, clock):
    with db.session_scope() as s:
        s.add(BatchJob(id="bat_pending", tenant_id=tenant.tenant_id, api_key_id=tenant.key_id,
                       status="queued", total_rows=1, input_csv="x", created_at=clock()))
        s.commit()
    resp = client.get(f"{BASE}/scans/batch/bat_pending/results", headers=tenant.headers)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "batch_not_completed"


def test_batch_idempotency(client, tenant, engine, db):
    first = _submit(client, tenant, headers={"Idempotency-Key": "batch-1"})
    calls = len(engine.analyze_calls)
    second = _submit(client, tenant, headers={"Idempotency-Key": "batch-1"})
    assert second.headers.get("idempotent-replayed") == "true"
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["status"] == "completed"  # current state, not the stale snapshot
    assert len(engine.analyze_calls) == calls
    with db.session_scope() as s:
        assert len(s.scalars(select(BatchJob)).all()) == 1


def test_batch_needs_scan_scope(client, make_tenant):
    assert _submit(client, make_tenant(scopes=("verify",))).status_code == 403
