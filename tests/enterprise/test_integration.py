"""
Full round trip: migrate an empty database with Alembic, provision a tenant
and key through the operator CLI, value a stone over HTTP, retrieve it by
id, and check the stored audit record and usage meter.

Runs on SQLite by default. Set ENTERPRISE_TEST_POSTGRES_URL to also run it
against a disposable Postgres database (never a production one).
"""

import json
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from enterprise.app import create_app, create_enterprise_app, export_openapi
from enterprise.cli import cli
from enterprise.db import Database
from enterprise.models import Valuation
from enterprise.settings import EnterpriseSettings, get_settings
from tests.enterprise.helpers import BASE, Clock

REPO = Path(__file__).resolve().parents[2]

DATABASES = ["sqlite"]
if os.environ.get("ENTERPRISE_TEST_POSTGRES_URL"):
    DATABASES.append("postgres")


@pytest.fixture(params=DATABASES)
def migrated_url(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'integration.db'}"
    else:
        url = os.environ["ENTERPRISE_TEST_POSTGRES_URL"]
    cfg = Config(str(REPO / "enterprise" / "alembic.ini"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()
    command.upgrade(cfg, "head")
    yield url
    command.downgrade(cfg, "base")


def _cli(url, *args):
    get_settings.cache_clear()
    result = CliRunner().invoke(cli, list(args), env={"ENTERPRISE_DATABASE_URL": url})
    get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[0])


def test_full_valuation_round_trip(migrated_url):
    db = Database(migrated_url)
    with db.engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert {"ent_tenants", "ent_api_keys", "ent_valuations", "ent_usage_daily"} <= tables

    tenant = _cli(migrated_url, "create-tenant", "--name", "Pilot Auction House",
                  "--environment", "sandbox")
    key = _cli(migrated_url, "create-key", "--tenant", tenant["tenant_id"], "--scopes", "comps")
    assert key["api_key"].startswith("gsk_test_")
    headers = {"Authorization": f"Bearer {key['api_key']}", "Idempotency-Key": "it-1"}

    settings = EnterpriseSettings(_env_file=None, database_url=migrated_url)
    app = create_app(settings, db=db, live_engine=None, clock=Clock())
    request = {
        "variety": "blue sapphire",
        "carat": 2.1,
        "shape": "oval",
        "color_grade": "intense",
        "clarity": "eye clean",
        "origin": "Sri Lanka",
        "valuation_basis": "retail_replacement",
    }
    with TestClient(app) as client:
        created = client.post(f"{BASE}/valuations", headers=headers, json=request)
        assert created.status_code == 201, created.text
        body = created.json()

        # Contract: band, comps with source type and similarity, confidence, scenarios, ids.
        assert body["status"] == "priced"
        assert body["synthetic_data"] is True
        band = body["price"]["per_carat"]
        assert 0 < band["low"] <= band["mid"] <= band["high"]
        assert body["price"]["total"]["mid"] == pytest.approx(band["mid"] * 2.1, abs=2)
        assert body["comps_summary"]["used"] >= 5
        assert {c["source_type"] for c in body["comps"]} <= {"realized", "asking"}
        assert all(0 <= c["similarity"] <= 1 and c["date"] for c in body["comps"])
        assert {s["scenario_id"] for s in body["scenarios"]} == {"treatment:none", "treatment:heated"}
        assert body["model_version"] == "comps-2026.11.0"
        assert body["valuation_id"].startswith("val_")

        # Audit retrieval returns exactly what was returned at creation.
        fetched = client.get(f"{BASE}/valuations/{body['valuation_id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json() == body

        # Idempotent retry does not create or bill a second valuation.
        replay = client.post(f"{BASE}/valuations", headers=headers, json=request)
        assert replay.json()["valuation_id"] == body["valuation_id"]

        usage = client.get(f"{BASE}/usage", headers=headers).json()
        assert usage["data"] == [
            {"date": "2026-11-24", "product": "comps", "valuations_total": 1,
             "valuations_priced": 1}
        ]

        spec = client.get(f"{BASE}/openapi.json").json()
        assert "/valuations" in spec["paths"] and "/valuations/batch" in spec["paths"]

    # The stored row holds full inputs, comps, model version and output.
    with db.session_scope() as s:
        row = s.scalars(select(Valuation)).one()
        assert row.tenant_id == tenant["tenant_id"]
        assert row.model_version == "comps-2026.11.0"
        assert row.request_payload["variety"] == "blue sapphire"
        assert row.result["comps"] == body["comps"]
        assert row.result["price"] == body["price"]
        assert row.engine == "sandbox"
        s.execute(text("SELECT 1"))
    db.engine.dispose()


def test_committed_openapi_spec_is_current():
    """docs/enterprise/openapi.json must be regenerated when the API changes:
    python -m enterprise.cli export-openapi"""
    settings = EnterpriseSettings(_env_file=None, database_url="sqlite://")
    app = create_enterprise_app(settings, live_engine=None)
    generated = export_openapi(app, settings.public_base_url)
    committed = json.loads((REPO / "docs" / "enterprise" / "openapi.json").read_text())
    assert committed == generated
