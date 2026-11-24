"""Confidence reason codes, scenarios and input normalization/validation."""

from datetime import date

import pytest

from enterprise.engine.base import StoneQuery
from enterprise.valuation.confidence import UsedComp, assess
from enterprise.valuation.scenarios import build_scenarios
from enterprise.valuation.vocab import normalize_lab, normalize_origin, normalize_treatment
from tests.enterprise.helpers import BASE, make_comp, stone

TODAY = date(2026, 11, 24)


def _q(**kw):
    base = dict(variety="blue_sapphire", carat=2.0, shape="oval", treatment="heated",
                origin="sri_lanka", lab="GIA")
    base.update(kw)
    return StoneQuery(**base)


def _used(n, **kw):
    base = dict(similarity=0.9, source_type="realized", observed_on=date(2026, 10, 1),
                origin="sri_lanka")
    base.update(kw)
    return [UsedComp(**base) for _ in range(n)]


def _codes(result):
    return [r["code"] for r in result["reasons"]]


BAND = {"low": 900, "mid": 1000, "high": 1100}


def test_clean_case_is_high_confidence():
    result = assess(query=_q(), used=_used(12), qualified_count=12, band_per_carat=BAND, today=TODAY)
    assert result == {"score": 1.0, "level": "high", "reasons": []}


@pytest.mark.parametrize(
    "kwargs,code",
    [
        (dict(qualified_count=6), "few_comps"),
        (dict(query=_q(treatment=None)), "missing_treatment"),
        (dict(query=_q(origin=None)), "missing_origin"),
        (dict(query=_q(origin="kashmir")), "rare_origin"),
        (dict(used=_used(12, source_type="asking")), "asking_only"),
        (dict(band_per_carat={"low": 500, "mid": 1000, "high": 1500}), "wide_band"),
        (dict(used=_used(12, observed_on=date(2023, 1, 1))), "stale_comps"),
        (dict(used=_used(12, similarity=0.6)), "low_similarity"),
        (dict(query=_q(lab=None)), "no_lab_report"),
    ],
)
def test_each_reason_code(kwargs, code):
    args = dict(query=_q(), used=_used(12), qualified_count=12, band_per_carat=BAND, today=TODAY)
    args.update(kwargs)
    result = assess(**args)
    assert code in _codes(result)
    assert result["score"] < 1.0


def test_missing_origin_only_for_origin_sensitive_varieties():
    result = assess(query=_q(variety="tanzanite", origin=None), used=_used(12),
                    qualified_count=12, band_per_carat=BAND, today=TODAY)
    assert "missing_origin" not in _codes(result)


def test_insufficient_has_zero_score_and_no_comp_statistics():
    result = assess(query=_q(), used=_used(2, source_type="asking", similarity=0.5),
                    qualified_count=2, band_per_carat=None, today=TODAY)
    assert result["score"] == 0 and result["level"] == "none"
    assert _codes(result)[0] == "insufficient_comps"
    assert "asking_only" not in _codes(result)
    assert "low_similarity" not in _codes(result)
    assert "2 qualifying comparables" in result["reasons"][0]["message"]


def test_scenarios_for_unknown_treatment_and_origin():
    ids = [s.scenario_id for s in build_scenarios(_q(treatment=None, origin=None))]
    assert ids[:2] == ["treatment:none", "treatment:heated"]
    assert "origin:kashmir" in ids and "origin:burma" in ids
    labels = {s.scenario_id: s.label for s in build_scenarios(_q(treatment=None, species="corundum"))}
    assert labels["treatment:none"] == "If unheated"
    emerald = [s.scenario_id for s in build_scenarios(_q(variety="emerald", treatment=None))]
    assert emerald[:2] == ["treatment:none", "treatment:oiled"]
    assert build_scenarios(_q()) == []


def test_api_returns_heated_vs_unheated_scenarios(client, tenant, live_engine):
    live_engine.by_treatment = {
        "none": [make_comp(i, ppc=3000) for i in range(6)],
        "heated": [make_comp(i, ppc=1000) for i in range(6)],
    }
    body = client.post(
        f"{BASE}/valuations", headers=tenant.headers,
        json=stone(treatment=None, valuation_basis="retail_replacement"),
    ).json()
    s = {x["scenario_id"]: x for x in body["scenarios"]}
    assert s["treatment:none"]["label"] == "If unheated"
    assert s["treatment:none"]["price"]["per_carat"]["mid"] == 3000
    assert s["treatment:heated"]["price"]["per_carat"]["mid"] == 1000
    assert s["treatment:heated"]["assumptions"] == {"treatment": "heated"}
    assert "missing_treatment" in _codes(body["confidence"])


# ── inputs ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("Unheated", "none"), ("no heat", "none"), ("H", "heated"), ("minor oil", "oiled"),
     ("unknown", None), (None, None), ("Be-diffused", None)],
)
def test_treatment_normalization(raw, expected):
    if raw == "Be-diffused":
        with pytest.raises(ValueError):
            normalize_treatment(raw)
    else:
        assert normalize_treatment(raw) == expected


def test_lab_and_origin_normalization():
    assert normalize_lab("Gübelin") == "Gubelin"
    assert normalize_lab("ssef") == "SSEF"
    with pytest.raises(ValueError):
        normalize_lab("Bob's Lab")
    assert normalize_origin("Myanmar") == "burma"
    assert normalize_origin("Ceylon") == "sri_lanka"
    assert normalize_origin("unknown") is None


def test_request_is_normalized_and_echoed(client, tenant, live_engine):
    body = client.post(
        f"{BASE}/valuations", headers=tenant.headers,
        json=stone(variety="Sapphire", origin="Ceylon", lab="Gübelin", treatment="no heat",
                   dimensions_mm={"length": 8.1, "width": 6.2, "depth": 4.0},
                   client_reference="LOT-77"),
    ).json()
    norm = body["normalized_inputs"]
    assert norm["variety"] == "blue_sapphire"
    assert norm["species"] == "corundum"
    assert norm["origin"] == "sri_lanka"
    assert norm["treatment"] == "none"
    assert norm["lab"] == "Gubelin"
    assert norm["length_mm"] == 8.1
    assert body["inputs"]["variety"] == "Sapphire"
    assert body["client_reference"] == "LOT-77"
    assert live_engine.calls[-1][0].variety == "blue_sapphire"


@pytest.mark.parametrize(
    "overrides",
    [
        {"variety": None},
        {"carat": None},
        {"shape": None},
        {"carat": 0},
        {"carat": -1},
        {"lab": "GIA", "lab_report_number": None},
        {"lab": None, "lab_report_number": "123456"},
        {"lab": "Bob's Lab"},
        {"treatment": "painted"},
        {"orgin": "typo-field"},
    ],
)
def test_invalid_requests_are_rejected(client, tenant, overrides):
    payload = stone(**{k: v for k, v in overrides.items() if k in stone()})
    for k, v in overrides.items():
        if v is None:
            payload.pop(k, None)
        else:
            payload[k] = v
    resp = client.post(f"{BASE}/valuations", headers=tenant.headers, json=payload)
    assert resp.status_code == 422, payload
    assert resp.json()["error"]["code"] == "invalid_request"
