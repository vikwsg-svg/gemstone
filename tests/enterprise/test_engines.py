"""Engine adapters: the V2 HTTP adapter contract and the synthetic sandbox."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from enterprise.app import create_app, live_engine_from_settings
from enterprise.engine.base import EngineUnavailable, ItemQuery, MetalSpec, StoneSpec
from enterprise.engine.remote import RemoteCompsEngine
from enterprise.engine.sandbox import SandboxEngine
from tests.enterprise.helpers import BASE, piece, stone


def _loose(**kw):
    s = dict(variety="blue_sapphire", carat=2.0, shape="oval", treatment="heated")
    s.update(kw)
    return ItemQuery(item_type="loose_stone", stones=(StoneSpec(**s),))


QUERY = _loose()


def _comp(i, **overrides):
    comp = {
        "comp_id": f"lst_{i}",
        "source_type": "asking",
        "source": "dealer:leibish",
        "observed_on": "2026-08-14",
        "currency": "USD",
        "price_total": 2000.0,
        "carat": 2.0,
        "similarity": 0.9,
        "attributes": {"variety": "blue_sapphire", "origin": "sri_lanka"},
    }
    comp.update(overrides)
    return comp


def _remote(handler):
    return RemoteCompsEngine("https://engine.internal", "secret", transport=httpx.MockTransport(handler))


def test_remote_engine_sends_contract_and_parses_comps():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"comps": [_comp(1), _comp(2, adjusted_price_per_carat=1200)]})

    result = _remote(handler).find_comps(QUERY, engine_version="v2.4", limit=60)
    assert seen["path"] == "/comps/search"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["engine_version"] == "v2.4"
    assert seen["body"]["limit"] == 60
    assert seen["body"]["query"]["item_type"] == "loose_stone"
    assert seen["body"]["query"]["stones"][0]["variety"] == "blue_sapphire"
    assert [c.comp_id for c in result.comps] == ["lst_1", "lst_2"]
    assert result.comps[1].adjusted_price_per_carat == 1200
    assert result.comps[0].price_per_carat == 1000
    assert result.breakdown_shares is None


def test_remote_engine_piece_comps_and_breakdown():
    ring = ItemQuery(item_type="ring", metal=MetalSpec(type="platinum", purity="950"))

    def handler(request):
        body = json.loads(request.content)
        assert body["query"]["metal"] == {"type": "platinum", "purity": "950", "color": None,
                                          "weight_grams": None}
        return httpx.Response(200, json={
            "comps": [_comp(1, carat=None, adjusted_price_total=4500.0)],
            "breakdown_shares": {"raw": 0.5, "make": 0.2, "market": 0.3},
        })

    result = _remote(handler).find_comps(ring, engine_version="v2.4", limit=10)
    assert result.comps[0].carat is None
    assert result.comps[0].adjusted_price_total == 4500.0
    assert result.breakdown_shares == {"raw": 0.5, "make": 0.2, "market": 0.3}


@pytest.mark.parametrize(
    "shares",
    [{"raw": 0.5, "make": 0.2}, {"raw": 0.9, "make": 0.9, "market": 0.1},
     {"raw": -0.1, "make": 0.6, "market": 0.5}, "junk"],
)
def test_remote_engine_ignores_bad_breakdown(shares):
    def handler(request):
        return httpx.Response(200, json={"comps": [_comp(1)], "breakdown_shares": shares})

    assert _remote(handler).find_comps(QUERY, engine_version="v2.4", limit=5).breakdown_shares is None


def test_remote_engine_drops_malformed_comps():
    def handler(request):
        return httpx.Response(200, json={"comps": [_comp(1), {"comp_id": "broken"}, "junk"]})

    result = _remote(handler).find_comps(QUERY, engine_version="v2.4", limit=60)
    assert [c.comp_id for c in result.comps] == ["lst_1"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={}),
        httpx.Response(200, json={"no_comps": []}),
        httpx.Response(200, content=b"not json"),
    ],
)
def test_remote_engine_failures_raise_unavailable(response):
    with pytest.raises(EngineUnavailable):
        _remote(lambda request: response).find_comps(QUERY, engine_version="v2.4", limit=5)


def test_remote_engine_network_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(EngineUnavailable):
        _remote(handler).find_comps(QUERY, engine_version="v2.4", limit=5)


def test_api_maps_engine_outage_to_503_and_rejects_untagged_comps(db, settings, clock, make_tenant):
    mode = {"fail": True}

    def handler(request):
        if mode["fail"]:
            return httpx.Response(502)
        comps = [_comp(i) for i in range(6)] + [_comp(99, source_type=None)]
        return httpx.Response(200, json={"comps": comps})

    app = create_app(settings, db=db, live_engine=_remote(handler), clock=clock)
    tenant = make_tenant()
    with TestClient(app) as c:
        down = c.post(f"{BASE}/valuations", headers=tenant.headers, json=stone())
        assert down.status_code == 503
        assert down.json()["error"]["code"] == "engine_unavailable"

        mode["fail"] = False
        body = c.post(f"{BASE}/valuations", headers=tenant.headers, json=stone()).json()
        assert body["status"] == "priced"
        assert body["comps_summary"]["rejected"] == 1
        assert body["synthetic_data"] is False


def test_live_engine_from_settings(settings):
    assert live_engine_from_settings(settings) is None
    remote = settings.model_copy(update={"engine_backend": "remote", "engine_url": "http://x"})
    assert isinstance(live_engine_from_settings(remote), RemoteCompsEngine)
    with pytest.raises(RuntimeError):
        live_engine_from_settings(settings.model_copy(update={"engine_backend": "remote"}))
    sandbox = settings.model_copy(update={"engine_backend": "sandbox"})
    assert isinstance(live_engine_from_settings(sandbox), SandboxEngine)


# ── sandbox ──────────────────────────────────────────────────────────────────


def test_sandbox_is_deterministic_and_tagged():
    a = SandboxEngine().find_comps(QUERY, engine_version="v2.4", limit=20).comps
    b = SandboxEngine().find_comps(QUERY, engine_version="v2.4", limit=20).comps
    assert a == b
    assert len(a) == 20
    assert all(c.source_type in ("realized", "asking") for c in a)
    assert all(c.attributes["synthetic"] is True for c in a)
    assert all(c.attributes["treatment"] == "heated" for c in a)  # known treatment filters
    assert [c.similarity for c in a] == sorted((c.similarity for c in a), reverse=True)


def test_sandbox_versions_differ_and_unknowns_are_empty():
    engine = SandboxEngine()
    v24 = engine.find_comps(QUERY, engine_version="v2.4", limit=10).comps
    v23 = engine.find_comps(QUERY, engine_version="v2.3", limit=10).comps
    assert [c.similarity for c in v24] != [c.similarity for c in v23]
    assert engine.supports("v2.4") and not engine.supports("v9")
    unknown = ItemQuery(item_type="loose_stone",
                        stones=(StoneSpec(variety="unobtainium", carat=1, shape="oval"),))
    assert engine.find_comps(unknown, engine_version="v2.4", limit=10).comps == []


def test_sandbox_diamonds_split_natural_and_lab_grown():
    engine = SandboxEngine()

    def mean_ppc(growth):
        q = _loose(variety="diamond", carat=1.0, shape="round", treatment=None, growth=growth)
        comps = engine.find_comps(q, engine_version="v2.4", limit=25).comps
        assert all(c.attributes["growth"] == growth for c in comps)
        return sum(c.price_per_carat for c in comps) / len(comps)

    assert mean_ppc("natural") > 5 * mean_ppc("lab_grown")


def test_sandbox_pieces_match_item_type_center_and_maker():
    engine = SandboxEngine()
    ring = ItemQuery(item_type="ring", metal=MetalSpec(type="platinum", weight_grams=6.0),
                     stones=(StoneSpec(variety="diamond", carat=1.0, growth="natural"),))
    result = engine.find_comps(ring, engine_version="v2.4", limit=30)
    assert result.comps and all(c.attributes["item_type"] == "ring" for c in result.comps)
    assert all(c.attributes["center_variety"] == "diamond"
               and c.attributes["center_growth"] == "natural" for c in result.comps)
    assert all(c.carat is None for c in result.comps)
    assert result.breakdown_shares is not None
    assert sum(result.breakdown_shares.values()) == pytest.approx(1.0)

    signed = ItemQuery(item_type="bracelet", metal=MetalSpec(type="gold"), brand="maison_aurelle",
                       signed=True)
    signed_comps = engine.find_comps(signed, engine_version="v2.4", limit=30).comps
    assert signed_comps and {c.attributes["brand"] for c in signed_comps} == {"maison_aurelle"}

    unknown_maker = ItemQuery(item_type="bracelet", metal=MetalSpec(type="gold"), brand="unknown_house")
    assert engine.find_comps(unknown_maker, engine_version="v2.4", limit=30).comps == []


def test_sandbox_thin_cells_trigger_floor(client, make_tenant):
    sandbox = make_tenant(environment="sandbox")
    body = client.post(
        f"{BASE}/valuations", headers=sandbox.headers,
        json=stone(variety="alexandrite", origin=None, treatment="none"),
    ).json()
    assert body["status"] == "insufficient_data"
    assert body["price"] is None


def test_sandbox_unknown_maker_is_insufficient_not_guessed(client, make_tenant):
    sandbox = make_tenant(environment="sandbox")
    body = client.post(
        f"{BASE}/valuations", headers=sandbox.headers,
        json=piece(item_type="bracelet", stones=None, brand="Some Famous House", signed=True,
                   metal={"type": "gold", "purity": "18k", "weight_grams": 25}),
    ).json()
    assert body["status"] == "insufficient_data"
    assert body["price"] is None and body["breakdown"] is None
