"""Model version stamping and pinning (request > tenant pin > default)."""

from datetime import date

import pytest

from enterprise.valuation.model_versions import (
    ModelRegistry,
    ModelVersion,
    UnknownModelVersion,
)
from tests.enterprise.helpers import BASE, stone

CURRENT = "comps-2026.11.0"
OLDER = "comps-2026.09.0"


def _value(client, tenant, **overrides):
    return client.post(f"{BASE}/valuations", headers=tenant.headers, json=stone(**overrides))


def test_default_version_is_stamped(client, tenant, live_engine):
    body = _value(client, tenant).json()
    assert body["model_version"] == CURRENT
    assert body["model_version_source"] == "default"
    assert body["engine_version"] == "v2.4"
    assert live_engine.calls[-1][1] == "v2.4"


def test_tenant_pin_is_used(client, make_tenant, live_engine):
    pinned = make_tenant(pin=OLDER)
    body = _value(client, pinned).json()
    assert body["model_version"] == OLDER
    assert body["model_version_source"] == "tenant_pin"
    assert live_engine.calls[-1][1] == "v2.3"
    assert body["comps_summary"]["similarity_threshold"] == 0.50


def test_request_pin_overrides_tenant_pin(client, make_tenant, live_engine):
    pinned = make_tenant(pin=OLDER)
    body = _value(client, pinned, model_version=CURRENT).json()
    assert body["model_version"] == CURRENT
    assert body["model_version_source"] == "request"
    assert live_engine.calls[-1][1] == "v2.4"


def test_request_pin_without_tenant_pin(client, tenant):
    body = _value(client, tenant, model_version=OLDER).json()
    assert body["model_version"] == OLDER
    assert body["model_version_source"] == "request"


def test_unknown_requested_version_is_rejected(client, tenant, live_engine):
    resp = _value(client, tenant, model_version="comps-1999.01.0")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "unknown_model_version"
    assert set(err["details"]["available"]) == {CURRENT, OLDER}
    assert live_engine.calls == []


def test_pinned_versions_reproduce_their_own_results(client, tenant):
    a1 = _value(client, tenant, model_version=OLDER).json()
    a2 = _value(client, tenant, model_version=OLDER).json()
    assert a1["price"] == a2["price"]
    assert a1["valuation_id"] != a2["valuation_id"]


def test_models_endpoint_reports_pin(client, make_tenant):
    pinned = make_tenant(pin=OLDER)
    body = client.get(f"{BASE}/models", headers=pinned.headers).json()
    assert body["default_model_version"] == CURRENT
    assert body["tenant_pinned_model_version"] == OLDER
    assert [m["id"] for m in body["data"]] == [CURRENT, OLDER]


def test_engine_not_serving_version_returns_503(client, tenant, live_engine):
    live_engine.supported = {"v2.4"}
    resp = _value(client, tenant, model_version=OLDER)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "model_version_unavailable"


# ── registry unit tests ──────────────────────────────────────────────────────


def _mv(id_, status, engine="v1"):
    return ModelVersion(id=id_, engine_version=engine, released=date(2026, 1, 1), status=status)


def test_registry_resolution_order():
    reg = ModelRegistry([_mv("a", "current"), _mv("b", "supported"), _mv("c", "supported")])
    assert reg.resolve(None, None) == (reg.get("a"), "default")
    assert reg.resolve(None, "b") == (reg.get("b"), "tenant_pin")
    assert reg.resolve("c", "b") == (reg.get("c"), "request")


def test_registry_rejects_retired_versions():
    reg = ModelRegistry([_mv("a", "current"), _mv("old", "retired")])
    with pytest.raises(UnknownModelVersion) as exc:
        reg.resolve("old", None)
    assert exc.value.available == ["a"]
    with pytest.raises(UnknownModelVersion):
        reg.resolve(None, "old")  # a tenant pinned to a retired version must be told


def test_registry_requires_exactly_one_current():
    with pytest.raises(ValueError):
        ModelRegistry([_mv("a", "current"), _mv("b", "current")])
    with pytest.raises(ValueError):
        ModelRegistry([_mv("a", "supported")])


def test_registry_default_override():
    reg = ModelRegistry([_mv("a", "current"), _mv("b", "supported")], default_id="b")
    assert reg.default.id == "b"
    with pytest.raises(ValueError):
        ModelRegistry([_mv("a", "current"), _mv("r", "retired")], default_id="r")


def test_model_version_validates_basis_factors():
    with pytest.raises(ValueError):
        ModelVersion(
            id="x", engine_version="v", released=date(2026, 1, 1), status="current",
            basis_factors={"asking": {"fair_market": 1.0}, "realized": {}},
        )
