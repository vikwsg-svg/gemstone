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
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from enterprise.app import create_app, create_enterprise_app, export_openapi
from enterprise.cli import cli
from enterprise.db import Base, Database
from enterprise.models import Scan
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


def test_full_scan_round_trip(migrated_url):
    db = Database(migrated_url)
    with db.engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert {"ent_tenants", "ent_api_keys", "ent_scans", "ent_usage_daily"} <= tables

    tenant = _cli(migrated_url, "create-tenant", "--name", "Pilot Auction House",
                  "--environment", "sandbox")
    key = _cli(migrated_url, "create-key", "--tenant", tenant["tenant_id"], "--scopes", "scan")
    assert key["api_key"].startswith("gsk_test_")
    headers = {"Authorization": f"Bearer {key['api_key']}", "Idempotency-Key": "it-1"}

    settings = EnterpriseSettings(_env_file=None, database_url=migrated_url)
    app = create_app(settings, db=db, live_engine=None, clock=Clock())
    request = {
        "photos": [{"url": "https://photos.example.com/ring-front.jpg"},
                   {"url": "https://photos.example.com/ring-inside-band.jpg"}],
        "details": {"item_type": "ring", "metal": {"type": "platinum", "weight_grams": 6.4}},
        "client_reference": "POLICY-88231/ITEM-2",
    }
    with TestClient(app) as client:
        created = client.post(f"{BASE}/scans", headers=headers, json=request)
        assert created.status_code == 201, created.text
        body = created.json()

        # Contract: identification, price context and comparables in one result.
        assert body["status"] == "completed"
        assert body["synthetic_data"] is True
        assert body["identification"]["status"] == "identified"
        assert body["identification"]["summary"].startswith("Platinum ring")
        price = body["price_context"]
        assert price["status"] == "provided" and 0 < price["low"] <= price["high"]
        assert body["comparables"]["total"] >= 5
        assert {c["price_type"] for c in body["comparables"]["items"]} <= {"realized", "asking"}
        assert body["model_version"] == "sandbox-2026.11"
        assert body["scan_id"].startswith("scn_")

        # Audit retrieval returns exactly what was returned at creation.
        fetched = client.get(f"{BASE}/scans/{body['scan_id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json() == body

        # Idempotent retry does not create or bill a second scan.
        replay = client.post(f"{BASE}/scans", headers=headers, json=request)
        assert replay.json()["scan_id"] == body["scan_id"]

        usage = client.get(f"{BASE}/usage", headers=headers).json()
        assert usage["data"] == [
            {"date": "2026-11-24", "product": "scan", "scans_total": 1,
             "scans_priced": 1}
        ]

        spec = client.get(f"{BASE}/openapi.json").json()
        assert "/scans" in spec["paths"] and "/scans/batch" in spec["paths"]

    # The stored row holds the inputs, the full result and the model version.
    with db.session_scope() as s:
        row = s.scalars(select(Scan)).one()
        assert row.tenant_id == tenant["tenant_id"]
        assert row.model_version == "sandbox-2026.11" and row.priced is True
        assert row.request_payload["details"]["metal"]["weight_grams"] == 6.4
        assert row.result == body
        assert row.engine == "sandbox"
        s.execute(text("SELECT 1"))
    db.engine.dispose()


def test_migrations_match_the_models(migrated_url):
    """Alembic head must create exactly what the ORM models describe."""
    db = Database(migrated_url)
    with db.engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={
            "compare_type": True,
            "include_object": lambda obj, name, type_, reflected, compare_to:
                type_ != "table" or (name.startswith("ent_") and name != "ent_alembic_version"),
        })
        assert compare_metadata(context, Base.metadata) == []
        tables = set(inspect(conn).get_table_names())
    assert {"ent_product_records", "ent_product_settings", "ent_photo_fingerprints",
            "ent_index_snapshots", "ent_index_corrections"} <= tables
    db.engine.dispose()


def test_committed_openapi_spec_is_current():
    """docs/enterprise/openapi.json must be regenerated when the API changes:
    python -m enterprise.cli export-openapi"""
    settings = EnterpriseSettings(_env_file=None, database_url="sqlite://")
    app = create_enterprise_app(settings, live_engine=None)
    generated = export_openapi(app, settings.public_base_url)
    committed = json.loads((REPO / "docs" / "enterprise" / "openapi.json").read_text())
    assert committed == generated
    website_copy = json.loads((REPO / "website" / "enterprise" / "openapi.json").read_text())
    assert website_copy == generated
