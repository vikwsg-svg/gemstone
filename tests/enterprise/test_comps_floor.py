"""The hard rule: fewer than 5 qualifying comps -> no price, comps returned, insufficient_data."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from enterprise.models import Valuation
from enterprise.schemas import ScenarioResult, ValuationResponse
from tests.enterprise.helpers import BASE, make_comp, stone


def _post(client, tenant, **overrides):
    return client.post(f"{BASE}/valuations", headers=tenant.headers, json=stone(**overrides))


def test_four_qualifying_comps_returns_no_price(client, tenant, live_engine, db):
    live_engine.comps = [make_comp(i) for i in range(4)]
    resp = _post(client, tenant)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "insufficient_data"
    assert body["price"] is None
    assert len(body["comps"]) == 4
    assert all(c["qualified"] for c in body["comps"])
    assert body["comps_summary"]["qualified"] == 4
    assert body["comps_summary"]["used"] == 0
    assert body["comps_summary"]["min_required"] == 5
    assert body["confidence"]["level"] == "none"
    assert body["confidence"]["score"] == 0
    assert body["confidence"]["reasons"][0]["code"] == "insufficient_comps"

    # Persisted like any other valuation.
    with db.session_scope() as s:
        row = s.scalars(select(Valuation)).one()
        assert row.status == "insufficient_data"
        assert row.result["price"] is None


def test_exactly_five_qualifying_comps_is_priced(client, tenant, live_engine):
    live_engine.comps = [make_comp(i) for i in range(5)]
    body = _post(client, tenant).json()
    assert body["status"] == "priced"
    assert body["price"]["per_carat"] == {"low": 800, "mid": 800, "high": 800}
    assert body["comps_summary"]["used"] == 5


def test_comps_below_similarity_threshold_do_not_count(client, tenant, live_engine):
    # 4 strong comps + 6 weak ones (threshold for the default model version is 0.55).
    live_engine.comps = [make_comp(i, similarity=0.9) for i in range(4)] + [
        make_comp(10 + i, similarity=0.4) for i in range(6)
    ]
    body = _post(client, tenant).json()
    assert body["status"] == "insufficient_data"
    assert body["price"] is None
    # Qualifying comps first, then the closest near misses for review.
    assert [c["qualified"] for c in body["comps"]] == [True] * 4 + [False] * 6
    assert body["comps_summary"]["considered"] == 10


@pytest.mark.parametrize(
    "bad",
    [
        {"source_type": ""},
        {"source_type": "estimate"},
        {"currency": "EUR"},
        {"ppc": 0.0},
    ],
)
def test_untagged_or_invalid_comps_are_rejected_not_priced(client, tenant, live_engine, bad):
    live_engine.comps = [make_comp(i) for i in range(4)] + [make_comp(99, **bad)]
    body = _post(client, tenant).json()
    assert body["status"] == "insufficient_data"
    assert body["comps_summary"]["rejected"] == 1
    assert any("excluded" in w for w in body["warnings"])


def test_scenarios_obey_the_floor(client, tenant, live_engine):
    live_engine.by_treatment = {
        "none": [make_comp(i, ppc=2000) for i in range(3)],  # too few unheated comps
        "heated": [make_comp(i, ppc=1000) for i in range(8)],
    }
    body = _post(client, tenant, treatment=None).json()
    scenarios = {s["scenario_id"]: s for s in body["scenarios"]}
    assert scenarios["treatment:none"]["status"] == "insufficient_data"
    assert scenarios["treatment:none"]["price"] is None
    assert scenarios["treatment:heated"]["status"] == "priced"
    assert scenarios["treatment:heated"]["price"]["per_carat"]["mid"] == 800


def test_response_schema_refuses_a_price_without_five_comps():
    """Defense in depth: even a buggy service cannot emit a price on < 5 comps."""
    band = {"low": 1, "mid": 2, "high": 3}
    with pytest.raises(ValidationError):
        ScenarioResult(
            scenario_id="x", label="x", assumptions={}, status="priced",
            price={"per_carat": band, "total": band}, comps_used=4,
            confidence={"score": 0.5, "level": "medium", "reasons": []},
        )
    with pytest.raises(ValidationError):
        ScenarioResult(
            scenario_id="x", label="x", assumptions={}, status="insufficient_data",
            price={"per_carat": band, "total": band}, comps_used=0,
            confidence={"score": 0, "level": "none", "reasons": []},
        )


def test_valuation_response_floor_validator(client, tenant, live_engine):
    live_engine.comps = [make_comp(i) for i in range(6)]
    body = _post(client, tenant).json()
    body["comps_summary"]["used"] = 4
    with pytest.raises(ValidationError):
        ValuationResponse.model_validate(body)
