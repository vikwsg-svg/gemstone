"""CSV batch valuations: submission, async processing, status and results CSV."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from enterprise.app import create_app
from enterprise.models import BatchJob, Scan
from tests.enterprise.helpers import BASE, make_comp

CSV_OK = """row_id,variety,carat,shape,treatment,origin,lab,lab_report_number,valuation_basis
A-1,blue sapphire,2.0,oval,heated,Sri Lanka,GIA,1234567890,
A-2,ruby,1.5,cushion,unheated,Burma,,,wholesale
A-3,blue sapphire,,oval,heated,,,,
=HYPERLINK("x"),blue sapphire,3.1,oval,heated,Sri Lanka,,,
"""


def _submit(client, tenant, text=CSV_OK, headers=None, **data):
    data.setdefault("valuation_basis", "fair_market")
    return client.post(
        f"{BASE}/scans/batch",
        headers={**tenant.headers, **(headers or {})},
        files={"file": ("stones.csv", text.encode("utf-8"), "text/csv")},
        data=data,
    )


def _results(client, tenant, job_id):
    resp = client.get(f"{BASE}/scans/batch/{job_id}/results", headers=tenant.headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(resp.text)))


def test_batch_round_trip(client, tenant, live_engine, db):
    resp = _submit(client, tenant)
    assert resp.status_code == 202
    job = resp.json()
    assert job["object"] == "batch_job"
    assert job["total_rows"] == 4
    assert job["model_version"] == "scan-2026.11.0"  # resolved and pinned at submission

    status = client.get(f"{BASE}/scans/batch/{job['job_id']}", headers=tenant.headers).json()
    assert status["status"] == "completed"
    assert status["processed_rows"] == 4
    assert status["priced_rows"] == 3
    assert status["failed_rows"] == 1
    assert status["results_url"] == f"{BASE}/scans/batch/{job['job_id']}/results"

    rows = _results(client, tenant, job["job_id"])
    assert [r["row_id"] for r in rows] == ["A-1", "A-2", "A-3", "'=HYPERLINK(\"x\")"]
    assert rows[0]["status"] == "priced"
    assert rows[0]["valuation_basis"] == "fair_market"  # job default
    assert rows[1]["valuation_basis"] == "wholesale"  # per-row override
    assert int(rows[1]["per_carat_mid"]) == 550  # asking 1000 x 0.55
    assert rows[2]["status"] == "error"
    assert "carat" in rows[2]["error"]
    assert rows[0]["model_version"] == "scan-2026.11.0"

    # Each priced row is a normal, retrievable, audited valuation linked to the job.
    valuation = client.get(
        f"{BASE}/scans/{rows[0]['scan_id']}", headers=tenant.headers
    ).json()
    assert valuation["batch_job_id"] == job["job_id"]
    assert valuation["client_reference"] == "A-1"
    with db.session_scope() as s:
        linked = s.scalars(select(Scan).where(Scan.batch_job_id == job["job_id"])).all()
        assert sorted(v.batch_row for v in linked) == [1, 2, 4]

    usage = client.get(f"{BASE}/usage", headers=tenant.headers).json()
    assert usage["data"][0]["scans_total"] == 3


def test_batch_reports_insufficient_rows(client, tenant, live_engine):
    live_engine.comps = [make_comp(i) for i in range(3)]
    job = _submit(client, tenant, "variety,carat,shape\nruby,1.0,oval\n").json()
    status = client.get(f"{BASE}/scans/batch/{job['job_id']}", headers=tenant.headers).json()
    assert status["insufficient_rows"] == 1
    row = _results(client, tenant, job["job_id"])[0]
    assert row["status"] == "insufficient_data"
    assert row["per_carat_mid"] == ""
    assert "insufficient_comps" in row["reason_codes"]


def test_batch_honours_model_version_pin(client, tenant, live_engine):
    job = _submit(
        client, tenant, "variety,carat,shape\nruby,1.0,oval\n", model_version="scan-2026.09.0"
    ).json()
    assert job["model_version"] == "scan-2026.09.0"
    assert live_engine.calls[-1][1] == "v2.3"


@pytest.mark.parametrize(
    "text,code",
    [
        ("colour,size\nred,big\n", "invalid_csv"),
        ("", "invalid_csv"),
        ("variety,carat,shape\n", "invalid_csv"),
    ],
)
def test_invalid_csv_is_rejected_up_front(client, tenant, text, code):
    resp = _submit(client, tenant, text)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_non_utf8_is_rejected(client, tenant):
    resp = client.post(
        f"{BASE}/scans/batch",
        headers=tenant.headers,
        files={"file": ("s.csv", "variety,carat,shape\nrubí,1,oval\n".encode("latin-1"))},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_csv"


def test_row_and_size_limits(db, settings, live_engine, clock, make_tenant):
    small = settings.model_copy(update={"batch_max_rows": 2, "batch_max_bytes": 200})
    tenant = make_tenant()
    with TestClient(create_app(small, db=db, live_engine=live_engine, clock=clock)) as c:
        too_many = _submit(c, tenant, "variety,carat,shape\n" + "ruby,1,oval\n" * 3)
        assert too_many.status_code == 422
        assert too_many.json()["error"]["code"] == "too_many_rows"
        too_big = _submit(c, tenant, "variety,carat,shape\n" + "ruby,1,oval," + "x" * 300 + "\n")
        assert too_big.status_code == 413


def test_results_before_completion_is_409(client, tenant, db, clock):
    with db.session_scope() as s:
        s.add(BatchJob(id="bat_pending", tenant_id=tenant.tenant_id, api_key_id=tenant.key_id,
                       status="queued", total_rows=1, input_csv="x", created_at=clock()))
        s.commit()
    resp = client.get(f"{BASE}/scans/batch/bat_pending/results", headers=tenant.headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "batch_not_completed"


def test_batch_idempotency(client, tenant, live_engine, db):
    first = _submit(client, tenant, headers={"Idempotency-Key": "batch-1"})
    calls_after_first = len(live_engine.calls)
    second = _submit(client, tenant, headers={"Idempotency-Key": "batch-1"})
    assert second.headers.get("idempotent-replayed") == "true"
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["status"] == "completed"  # current state, not the stale snapshot
    assert len(live_engine.calls) == calls_after_first  # not re-run
    with db.session_scope() as s:
        assert len(s.scalars(select(BatchJob)).all()) == 1


def test_batch_needs_comps_scope(client, make_tenant):
    other = make_tenant(scopes=("index",))
    assert _submit(client, other).status_code == 403
