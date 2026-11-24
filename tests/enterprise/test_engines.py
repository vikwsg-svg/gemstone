"""Engine adapters: the V2 HTTP adapter contract and the synthetic sandbox."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from enterprise.app import create_app, live_engine_from_settings
from enterprise.engine.base import EngineUnavailable, StoneQuery
from enterprise.engine.remote import RemoteCompsEngine
from enterprise.engine.sandbox import SandboxEngine
from tests.enterprise.helpers import BASE, stone

QUERY = StoneQuery(variety="blue_sapphire", carat=2.0, shape="oval", treatment="heated")


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

    comps = _remote(handler).find_comps(QUERY, engine_version="v2.4", limit=60)
    assert seen["path"] == "/comps/search"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["engine_version"] == "v2.4"
    assert seen["body"]["limit"] == 60
    assert seen["body"]["query"]["variety"] == "blue_sapphire"
    assert [c.comp_id for c in comps] == ["lst_1", "lst_2"]
    assert comps[1].adjusted_price_per_carat == 1200
    assert comps[0].price_per_carat == 1000


def test_remote_engine_drops_malformed_comps():
    def handler(request):
        return httpx.Response(200, json={"comps": [_comp(1), {"comp_id": "broken"}, "junk"]})

    comps = _remote(handler).find_comps(QUERY, engine_version="v2.4", limit=60)
    assert [c.comp_id for c in comps] == ["lst_1"]


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
    engine = SandboxEngine()
    a = engine.find_comps(QUERY, engine_version="v2.4", limit=20)
    b = SandboxEngine().find_comps(QUERY, engine_version="v2.4", limit=20)
    assert a == b
    assert len(a) == 20
    assert all(c.source_type in ("realized", "asking") for c in a)
    assert all(c.attributes["synthetic"] is True for c in a)
    assert all(c.attributes["treatment"] == "heated" for c in a)  # known treatment filters
    assert [c.similarity for c in a] == sorted((c.similarity for c in a), reverse=True)


def test_sandbox_versions_differ_and_unknowns_are_empty():
    engine = SandboxEngine()
    v24 = engine.find_comps(QUERY, engine_version="v2.4", limit=10)
    v23 = engine.find_comps(QUERY, engine_version="v2.3", limit=10)
    assert [c.similarity for c in v24] != [c.similarity for c in v23]
    assert engine.supports("v2.4") and not engine.supports("v9")
    assert engine.find_comps(
        StoneQuery(variety="unobtainium", carat=1, shape="oval"), engine_version="v2.4", limit=10
    ) == []


def test_sandbox_thin_cells_trigger_floor(client, make_tenant):
    sandbox = make_tenant(environment="sandbox")
    body = client.post(
        f"{BASE}/valuations",
        headers=sandbox.headers,
        json=stone(variety="alexandrite", origin=None, treatment="none"),
    ).json()
    assert body["status"] == "insufficient_data"
    assert body["price"] is None
