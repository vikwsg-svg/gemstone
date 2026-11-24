"""Confidence reason codes, scenarios and input normalization/validation."""

from datetime import date

import pytest

from enterprise.engine.base import ItemQuery, MetalSpec, StoneSpec
from enterprise.valuation.confidence import UsedComp, assess
from enterprise.valuation.scenarios import build_scenarios
from enterprise.valuation.vocab import (
    normalize_growth,
    normalize_lab,
    normalize_origin,
    normalize_purity,
    normalize_treatment,
)
from tests.enterprise.helpers import BASE, make_comp, piece, stone

TODAY = date(2026, 11, 24)


def _stone(**kw):
    base = dict(variety="blue_sapphire", carat=2.0, shape="oval", treatment="heated",
                origin="sri_lanka", lab="GIA", species="corundum")
    base.update(kw)
    return StoneSpec(**base)


def _q(item_type="loose_stone", metal=None, **stone_kw):
    return ItemQuery(item_type=item_type, stones=(_stone(**stone_kw),), metal=metal)


def _used(n, **kw):
    base = dict(similarity=0.9, source_type="realized", observed_on=date(2026, 10, 1),
                origin="sri_lanka")
    base.update(kw)
    return [UsedComp(**base) for _ in range(n)]


def _codes(result):
    return [r["code"] for r in result["reasons"]]


BAND = {"low": 900, "mid": 1000, "high": 1100}


def _assess(**kwargs):
    args = dict(query=_q(), used=_used(12), qualified_count=12, band_mid_low_high=BAND, today=TODAY)
    args.update(kwargs)
    return assess(**args)


def test_clean_case_is_high_confidence():
    assert _assess() == {"score": 1.0, "level": "high", "reasons": []}


@pytest.mark.parametrize(
    "kwargs,code",
    [
        (dict(qualified_count=6), "few_comps"),
        (dict(query=_q(treatment=None)), "missing_treatment"),
        (dict(query=_q(origin=None)), "missing_origin"),
        (dict(query=_q(origin="kashmir")), "rare_origin"),
        (dict(query=_q(variety="diamond", species="diamond", treatment=None, origin=None,
                       growth=None)), "missing_growth"),
        (dict(used=_used(12, source_type="asking")), "asking_only"),
        (dict(band_mid_low_high={"low": 500, "mid": 1000, "high": 1500}), "wide_band"),
        (dict(used=_used(12, observed_on=date(2023, 1, 1))), "stale_comps"),
        (dict(used=_used(12, similarity=0.6)), "low_similarity"),
        (dict(query=_q(lab=None)), "no_lab_report"),
    ],
)
def test_each_reason_code(kwargs, code):
    result = _assess(**kwargs)
    assert code in _codes(result)
    assert result["score"] < 1.0


def test_missing_treatment_weighs_more_where_treatment_drives_price():
    sapphire = _assess(query=_q(treatment=None))
    tanzanite = _assess(query=_q(variety="tanzanite", species="zoisite", treatment=None))
    emerald = _assess(query=_q(variety="emerald", species="beryl", treatment=None, origin="colombia"),
                      used=_used(12, origin="colombia"))
    assert sapphire["score"] == emerald["score"] == 0.7
    assert sapphire["level"] == "medium"
    assert tanzanite["score"] == 0.85


def test_unknown_diamond_growth_is_low_confidence_and_no_treatment_reason():
    q = _q(variety="diamond", species="diamond", treatment=None, origin=None, growth=None)
    result = _assess(query=q, band_mid_low_high={"low": 300, "mid": 3000, "high": 4000})
    assert result["level"] == "low"
    assert "missing_treatment" not in _codes(result)


def test_missing_origin_only_for_origin_sensitive_varieties():
    result = _assess(query=_q(variety="tanzanite", origin=None))
    assert "missing_origin" not in _codes(result)


def test_piece_reasons():
    metal = MetalSpec(type="gold", purity="18k")
    ring = ItemQuery(item_type="ring", stones=(_stone(carat=0.5, lab=None),), metal=metal,
                     brand="maison_aurelle")
    codes = _codes(_assess(query=ring))
    assert "missing_metal_weight" in codes
    assert "unsigned_brand" in codes
    assert "missing_condition" in codes
    assert "no_lab_report" not in codes  # small set stones are not expected to be certified

    no_metal = ItemQuery(item_type="pendant", stones=(_stone(),), condition="excellent")
    assert "missing_metal" in _codes(_assess(query=no_metal))

    signed = ItemQuery(item_type="bracelet", metal=MetalSpec(type="gold", weight_grams=20),
                       brand="maison_aurelle", signed=True, condition="good")
    assert _codes(_assess(query=signed)) == []


def test_insufficient_has_zero_score_and_no_comp_statistics():
    result = _assess(used=_used(2, source_type="asking", similarity=0.5), qualified_count=2,
                     band_mid_low_high=None)
    assert result["score"] == 0 and result["level"] == "none"
    assert _codes(result)[0] == "insufficient_comps"
    assert "asking_only" not in _codes(result)
    assert "low_similarity" not in _codes(result)
    assert "2 qualifying comparables" in result["reasons"][0]["message"]


def test_scenarios_for_unknowns():
    ids = [s.scenario_id for s in build_scenarios(_q(treatment=None, origin=None))]
    assert ids[:2] == ["treatment:none", "treatment:heated"]
    assert "origin:kashmir" in ids and "origin:burma" in ids
    labels = {s.scenario_id: s.label for s in build_scenarios(_q(treatment=None))}
    assert labels["treatment:none"] == "If unheated"
    emerald = [s.scenario_id for s in build_scenarios(_q(variety="emerald", treatment=None))]
    assert emerald[:2] == ["treatment:none", "treatment:oiled"]
    diamond = build_scenarios(_q(variety="diamond", treatment=None, origin=None, growth=None))
    assert [(s.scenario_id, s.label) for s in diamond] == [
        ("growth:natural", "If natural"), ("growth:lab_grown", "If lab-grown")]
    assert diamond[1].query.principal.growth == "lab_grown"
    assert build_scenarios(_q()) == []
    assert build_scenarios(ItemQuery(item_type="bracelet", metal=MetalSpec(type="gold"))) == []


def test_scenarios_target_the_center_stone_of_a_piece():
    side = StoneSpec(variety="diamond", carat=0.5, count=10, role="side", growth="natural")
    center = _stone(role="center", treatment=None)
    ring = ItemQuery(item_type="ring", stones=(side, center), metal=MetalSpec(type="platinum"))
    scen = {s.scenario_id: s for s in build_scenarios(ring)}
    heated = scen["treatment:heated"].query
    assert heated.principal.treatment == "heated"
    assert heated.stones[0] == side  # side stones untouched


def test_api_returns_heated_vs_unheated_scenarios(client, tenant, live_engine):
    live_engine.by_treatment = {
        "none": [make_comp(i, ppc=3000) for i in range(6)],
        "heated": [make_comp(i, ppc=1000) for i in range(6)],
    }
    body = client.post(
        f"{BASE}/scans", headers=tenant.headers,
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
     ("unknown", None), (None, None)],
)
def test_treatment_normalization(raw, expected):
    assert normalize_treatment(raw) == expected


def test_vocab_normalization():
    with pytest.raises(ValueError):
        normalize_treatment("Be-diffused")
    assert normalize_lab("Gübelin") == "Gubelin"
    assert normalize_lab("igi") == "IGI"
    with pytest.raises(ValueError):
        normalize_lab("Bob's Lab")
    assert normalize_origin("Myanmar") == "burma"
    assert normalize_origin("Ceylon") == "sri_lanka"
    assert normalize_origin("unknown") is None
    assert normalize_growth("Lab-created") == "lab_grown"
    assert normalize_growth("CVD") == "lab_grown"
    assert normalize_growth("earth mined") == "natural"
    assert normalize_purity("750") == "18k"
    assert normalize_purity("PT950") == "950"
    assert normalize_purity("Sterling") == "925"


def test_loose_stone_request_is_normalized_and_echoed(client, tenant, live_engine):
    body = client.post(
        f"{BASE}/scans", headers=tenant.headers,
        json=stone(variety="Sapphire", origin="Ceylon", lab="Gübelin", treatment="no heat",
                   dimensions_mm={"length": 8.1, "width": 6.2, "depth": 4.0},
                   client_reference="LOT-77"),
    ).json()
    norm = body["item"]["stones"][0]
    assert body["item_type"] == "loose_stone"
    assert norm["variety"] == "blue_sapphire"
    assert norm["species"] == "corundum"
    assert norm["origin"] == "sri_lanka"
    assert norm["treatment"] == "none"
    assert norm["lab"] == "Gubelin"
    assert norm["length_mm"] == 8.1
    assert body["inputs"]["stones"][0]["variety"] == "Sapphire"
    assert body["client_reference"] == "LOT-77"
    assert live_engine.calls[-1][0].principal.variety == "blue_sapphire"


def test_piece_request_is_normalized(client, tenant, live_engine):
    body = client.post(
        f"{BASE}/scans", headers=tenant.headers,
        json=piece(metal={"type": "Yellow Gold", "purity": "750", "color": "Yellow",
                          "weight_grams": 4.2},
                   stones=[{"variety": "Diamonds", "carat": 1.1, "shape": "Round",
                            "growth": "lab-created", "color_grade": "F", "clarity": "VVS2"}],
                   brand="Maison Aurelle", signed=True, era="Art Deco"),
    ).json()
    norm = body["item"]
    assert norm["metal"] == {"type": "gold", "purity": "18k", "color": "yellow", "weight_grams": 4.2}
    assert norm["stones"][0]["variety"] == "diamond"
    assert norm["stones"][0]["species"] == "diamond"
    assert norm["stones"][0]["growth"] == "lab_grown"
    assert norm["stones"][0]["role"] == "center"
    assert norm["brand"] == "maison_aurelle"
    assert norm["era"] == "art_deco"
    assert body["item_type"] == "ring"


def _with(payload, path, value):
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is None:
        target.pop(path[-1], None)
    else:
        target[path[-1]] = value
    return payload


@pytest.mark.parametrize(
    "build",
    [
        lambda: _with(stone(), ("stones", 0, "variety"), None),
        lambda: _with(stone(), ("stones", 0, "carat"), None),
        lambda: _with(stone(), ("stones", 0, "shape"), None),  # loose stone needs a shape
        lambda: _with(stone(), ("stones", 0, "carat"), 0),
        lambda: _with(stone(), ("stones", 0, "carat"), -1),
        lambda: _with(stone(), ("stones", 0, "lab_report_number"), None),
        lambda: _with(stone(), ("stones", 0, "lab"), "Bob's Lab"),
        lambda: _with(stone(), ("stones", 0, "treatment"), "painted"),
        lambda: _with(stone(), ("stones", 0, "growth"), "maybe"),
        lambda: _with(stone(), ("stones", 0, "orgin"), "typo-field"),
        lambda: _with(stone(), ("item_type",), None),
        lambda: _with(stone(), ("item_type",), "watch"),
        lambda: _with(stone(), ("metal",), {"type": "gold"}),  # loose stone with metal
        lambda: _with(stone(), ("stones",), []),
        lambda: _with(stone(), ("stones",), stone()["stones"] * 2),
        lambda: _with(piece(), ("stones",), [{"variety": "diamond", "carat": 1, "role": "center"},
                                             {"variety": "ruby", "carat": 1, "role": "center"}]),
        lambda: _with(_with(piece(), ("metal",), None), ("stones",), []),  # piece with nothing
        lambda: _with(piece(), ("metal",), {"type": "copper"}),
        lambda: _with(piece(), ("condition",), "like new"),
    ],
)
def test_invalid_requests_are_rejected(client, tenant, build):
    payload = build()
    resp = client.post(f"{BASE}/scans", headers=tenant.headers, json=payload)
    assert resp.status_code == 422, payload
    assert resp.json()["error"]["code"] == "invalid_request"
