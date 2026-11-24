"""Valuation basis handling and band math."""

import pytest

from enterprise.valuation.model_versions import DEFAULT_BASIS_FACTORS
from enterprise.valuation.service import weighted_quantile
from tests.enterprise.helpers import BASE, make_comp, stone


def _value(client, tenant, **overrides):
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=stone(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("basis", list(DEFAULT_BASIS_FACTORS["asking"]))
def test_asking_comps_are_converted_to_each_basis(client, tenant, live_engine, basis):
    live_engine.comps = [make_comp(i, source_type="asking", ppc=1000) for i in range(6)]
    body = _value(client, tenant, valuation_basis=basis)
    factor = DEFAULT_BASIS_FACTORS["asking"][basis]
    expected = round(1000 * factor)
    assert body["valuation_basis"] == basis
    assert body["price"]["per_carat"] == {"low": expected, "mid": expected, "high": expected}
    assert body["price"]["total"]["mid"] == round(1000 * factor * 2.0)
    assert all(c["basis_factor"] == factor for c in body["comps"])
    assert all(c["basis_price_per_carat"] == expected for c in body["comps"])


@pytest.mark.parametrize("basis", list(DEFAULT_BASIS_FACTORS["realized"]))
def test_realized_comps_are_converted_to_each_basis(client, tenant, live_engine, basis):
    live_engine.comps = [make_comp(i, source_type="realized", ppc=1000) for i in range(6)]
    body = _value(client, tenant, valuation_basis=basis)
    expected = round(1000 * DEFAULT_BASIS_FACTORS["realized"][basis])
    assert body["price"]["per_carat"]["mid"] == expected
    assert body["comps_summary"]["realized"] == 6
    assert body["comps_summary"]["asking"] == 0


def test_same_comps_give_different_prices_per_basis(client, tenant, live_engine):
    live_engine.comps = [make_comp(i, source_type="asking", ppc=1000) for i in range(6)]
    mids = {
        basis: _value(client, tenant, valuation_basis=basis)["price"]["per_carat"]["mid"]
        for basis in ("retail_replacement", "fair_market", "wholesale")
    }
    assert mids["retail_replacement"] > mids["fair_market"] > mids["wholesale"]


def test_mixed_sources_band_spans_converted_prices(client, tenant, live_engine):
    # fair_market: realized x1.00 -> 1000/ct, asking x0.80 -> 800/ct.
    live_engine.comps = [make_comp(i, source_type="realized", ppc=1000) for i in range(5)] + [
        make_comp(10 + i, source_type="asking", ppc=1000) for i in range(5)
    ]
    band = _value(client, tenant)["price"]["per_carat"]
    assert 800 <= band["low"] <= band["mid"] <= band["high"] <= 1000
    # Realized comps carry more weight, so the midpoint sits above the simple average.
    assert band["mid"] > 900


def test_engine_adjusted_price_is_used_when_present(client, tenant, live_engine):
    live_engine.comps = [make_comp(i, ppc=1000, adjusted=1500) for i in range(6)]
    body = _value(client, tenant, valuation_basis="retail_replacement")
    assert body["price"]["per_carat"]["mid"] == 1500
    assert body["comps"][0]["price_per_carat"] == 1000
    assert body["comps"][0]["adjusted_price_per_carat"] == 1500


def test_total_uses_subject_carat(client, tenant, live_engine):
    live_engine.comps = [make_comp(i, ppc=1000) for i in range(6)]
    body = _value(client, tenant, carat=3.5, valuation_basis="retail_replacement")
    assert body["price"]["total"] == {"low": 3500, "mid": 3500, "high": 3500}


@pytest.mark.parametrize("basis", [None, "replacement", "market"])
def test_missing_or_unknown_basis_is_rejected(client, tenant, basis):
    payload = stone()
    if basis is None:
        payload.pop("valuation_basis")
    else:
        payload["valuation_basis"] = basis
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_weighted_quantile():
    assert weighted_quantile([10, 20, 30], [1, 1, 1], 0.5) == 20
    assert weighted_quantile([10, 20, 30], [1, 1, 1], 0.0) == 10
    assert weighted_quantile([10, 20, 30], [1, 1, 1], 1.0) == 30
    # Heavier weight pulls the median.
    assert weighted_quantile([10, 20, 30], [1, 1, 10], 0.5) > 25
    assert weighted_quantile([10, 20, 30], [10, 1, 1], 0.5) < 15
    # Interpolates between neighbours.
    assert 10 < weighted_quantile([10, 20], [1, 1], 0.5) < 20
    with pytest.raises(ValueError):
        weighted_quantile([], [], 0.5)
