"""Finished pieces: total band, Raw/Make/Market breakdown, piece comps, growth scenarios, batch."""

import csv
import io

import pytest

from tests.enterprise.helpers import BASE, make_comp, make_piece_comp, piece, stone

SHARES = {"raw": 0.5, "make": 0.2, "market": 0.3}


def _value(client, tenant, payload):
    resp = client.post(f"{BASE}/valuations", headers=tenant.headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_piece_is_priced_per_item_with_breakdown(client, tenant, live_engine):
    live_engine.comps = [make_piece_comp(i, price=5000) for i in range(6)]
    live_engine.breakdown_shares = SHARES
    body = _value(client, tenant, piece())  # retail_replacement: asking x 1.00

    assert body["item_type"] == "ring"
    assert body["status"] == "priced"
    assert body["price"]["per_carat"] is None
    assert body["price"]["total"] == {"low": 5000, "mid": 5000, "high": 5000}
    assert body["breakdown"] == {
        "raw": {"share": 0.5, "value": 2500},
        "make": {"share": 0.2, "value": 1000},
        "market": {"share": 0.3, "value": 1500},
    }
    comp = body["comps"][0]
    assert comp["carat"] is None and comp["price_per_carat"] is None
    assert comp["basis_price_per_carat"] is None
    assert comp["basis_price_total"] == 5000


def test_piece_basis_and_adjusted_totals(client, tenant, live_engine):
    live_engine.comps = [make_piece_comp(i, price=5000, adjusted=6000) for i in range(6)]
    body = _value(client, tenant, piece(valuation_basis="wholesale"))
    assert body["price"]["total"]["mid"] == 3300  # 6000 adjusted x 0.55 wholesale
    assert body["comps"][0]["adjusted_price_total"] == 6000
    assert body["breakdown"] is None  # engine gave no shares


def test_piece_floor_applies_and_drops_breakdown(client, tenant, live_engine):
    live_engine.comps = [make_piece_comp(i) for i in range(4)]
    live_engine.breakdown_shares = SHARES
    body = _value(client, tenant, piece())
    assert body["status"] == "insufficient_data"
    assert body["price"] is None and body["breakdown"] is None


def test_loose_stone_never_gets_a_breakdown(client, tenant, live_engine):
    live_engine.breakdown_shares = SHARES
    body = _value(client, tenant, stone())
    assert body["breakdown"] is None
    assert body["price"]["per_carat"] is not None


def test_loose_comps_need_a_carat_but_piece_comps_do_not(client, tenant, live_engine):
    live_engine.comps = [make_piece_comp(i) for i in range(6)]  # carat None
    loose = _value(client, tenant, stone())
    assert loose["status"] == "insufficient_data"
    assert loose["comps_summary"]["rejected"] == 6
    assert _value(client, tenant, piece())["status"] == "priced"


def test_diamond_growth_scenarios(client, tenant, live_engine):
    live_engine.by_growth = {
        "natural": [make_comp(i, ppc=5000) for i in range(6)],
        "lab_grown": [make_comp(i, ppc=400) for i in range(6)],
    }
    payload = stone(variety="diamond", treatment=None, origin=None, shape="round",
                    color_grade="G", clarity="VS1", carat=1.0, valuation_basis="retail_replacement")
    body = _value(client, tenant, payload)
    s = {x["scenario_id"]: x for x in body["scenarios"]}
    assert s["growth:natural"]["label"] == "If natural"
    assert s["growth:natural"]["price"]["per_carat"]["mid"] == 5000
    assert s["growth:lab_grown"]["label"] == "If lab-grown"
    assert s["growth:lab_grown"]["price"]["per_carat"]["mid"] == 400
    assert "missing_growth" in [r["code"] for r in body["confidence"]["reasons"]]
    assert not [x for x in body["scenarios"] if x["scenario_id"].startswith("treatment:")]


def test_sandbox_values_a_ring_end_to_end(client, make_tenant):
    sandbox = make_tenant(environment="sandbox")
    body = _value(client, sandbox, piece(
        metal={"type": "platinum", "purity": "950", "weight_grams": 6.4},
        stones=[{"role": "center", "variety": "blue sapphire", "carat": 2.1, "shape": "oval",
                 "origin": "Sri Lanka"},
                {"role": "side", "variety": "diamond", "carat": 0.6, "count": 14,
                 "growth": "natural"}],
    ))
    assert body["status"] == "priced"
    assert body["synthetic_data"] is True
    assert body["price"]["per_carat"] is None
    parts = body["breakdown"]
    assert sum(p["share"] for p in parts.values()) == pytest.approx(1.0, abs=0.005)
    assert {s["scenario_id"] for s in body["scenarios"]} == {"treatment:none", "treatment:heated"}
    unheated = next(s for s in body["scenarios"] if s["scenario_id"] == "treatment:none")
    heated = next(s for s in body["scenarios"] if s["scenario_id"] == "treatment:heated")
    assert unheated["price"]["total"]["mid"] > heated["price"]["total"]["mid"]
    assert all(c["attributes"]["item_type"] == "ring" for c in body["comps"])


def test_batch_with_pieces_and_loose_stones(client, tenant, live_engine):
    live_engine.comps = [make_piece_comp(i, price=5000) for i in range(3)] + [
        make_comp(i, ppc=1000) for i in range(6)
    ]
    live_engine.breakdown_shares = SHARES
    text = (
        "row_id,item_type,variety,carat,shape,growth,metal_type,metal_purity,metal_weight_grams,"
        "side_carat_total,side_count,brand,signed,condition\n"
        "R1,ring,diamond,1.0,round,natural,platinum,950,6,0.4,12,,,excellent\n"
        "B1,bracelet,,,,,gold,750,25,,,Maison Aurelle,yes,good\n"
        "L1,,blue sapphire,2,oval,,,,,,,,,\n"
        "X1,ring,,,,,,,,,,,,\n"
    )
    job = client.post(
        f"{BASE}/valuations/batch", headers=tenant.headers,
        files={"file": ("mixed.csv", text, "text/csv")}, data={"valuation_basis": "fair_market"},
    ).json()
    rows = list(csv.DictReader(io.StringIO(client.get(
        f"{BASE}/valuations/batch/{job['job_id']}/results", headers=tenant.headers).text)))
    by_id = {r["row_id"]: r for r in rows}

    assert by_id["R1"]["item_type"] == "ring" and by_id["R1"]["status"] == "priced"
    assert by_id["R1"]["per_carat_mid"] == ""  # pieces have no per-carat band
    assert by_id["R1"]["breakdown_make"] != ""
    assert by_id["B1"]["item_type"] == "bracelet" and by_id["B1"]["status"] == "priced"
    assert by_id["L1"]["item_type"] == "loose_stone" and by_id["L1"]["per_carat_mid"] == "800"
    assert by_id["X1"]["status"] == "error"  # a ring with neither metal nor stones
    assert "metal, stones" in by_id["X1"]["error"]

    ring_query = next(q for q, _ in live_engine.calls if q.item_type == "ring")
    assert ring_query.metal.purity == "950"
    assert [s.role for s in ring_query.stones] == ["center", "side"]
    assert ring_query.stones[1].count == 12
    bracelet_query = next(q for q, _ in live_engine.calls if q.item_type == "bracelet")
    assert bracelet_query.signed is True and bracelet_query.brand == "maison_aurelle"
    assert bracelet_query.metal.purity == "18k"
