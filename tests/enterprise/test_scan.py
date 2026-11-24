"""The Scan API: identify (photos and/or details), then price."""

import base64
import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from enterprise.app import create_app
from enterprise.engine.base import Identification
from enterprise.models import Scan
from enterprise.scan import merge_item
from tests.enterprise.helpers import (
    BASE,
    RING_IDENTIFICATION,
    make_piece_comp,
    photo_url,
)

BASIS = {"valuation_basis": "retail_replacement"}


def _scan(client, tenant, **payload):
    return client.post(f"{BASE}/scans", headers=tenant.headers, json={**BASIS, **payload})


@pytest.fixture
def piece_comps(live_engine):
    live_engine.comps = [make_piece_comp(i, price=5000) for i in range(8)]
    return live_engine


def test_photo_scan_identifies_then_prices(client, tenant, identifier, piece_comps, db):
    resp = _scan(client, tenant, photos=photo_url(2))
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["object"] == "scan" and body["product"] == "scan"
    assert body["status"] == "priced"
    assert body["item_type"] == "ring"
    ident = body["identification"]
    assert ident["source"] == "photos"
    assert ident["status"] == "identified"
    assert ident["confidence"] == 0.9
    assert ident["summary"] == RING_IDENTIFICATION.summary
    assert ident["observations"] == list(RING_IDENTIFICATION.observations)
    assert ident["alternatives"] == [{"label": "Center stone is moissanite", "probability": 0.05}]
    assert ident["open_questions"] and ident["next_evidence"]
    assert ident["field_sources"] == {"item_type": "identified", "metal": "identified",
                                      "stones": "identified", "condition": "identified"}
    assert [p["url"] for p in ident["photos"]] == [p["url"] for p in photo_url(2)]
    assert all(len(p["sha256"]) == 64 for p in ident["photos"])

    # The identified item is what got priced.
    assert body["item"]["item_type"] == "ring"
    query = piece_comps.calls[-1][0]
    assert query.item_type == "ring" and query.principal.variety == "diamond"
    assert body["price"]["total"]["mid"] == 5000

    with db.session_scope() as s:
        row = s.scalars(select(Scan)).one()
        assert row.status == "priced" and row.photo_count == 2
        assert row.identifier == "fake-identify"


def test_inline_photo_is_stored_as_metadata_only(client, tenant, identifier, piece_comps, db):
    data = base64.b64encode(b"\xff\xd8" + b"jpeg-bytes" * 300).decode()
    body = _scan(client, tenant, photos=[{"data": data, "media_type": "image/jpeg"}]).json()
    stored = body["inputs"]["photos"][0]
    assert stored["bytes"] == 3002 and stored["media_type"] == "image/jpeg"
    assert "data" not in stored
    sent = identifier.calls[-1][0][0]
    assert sent.data is not None and sent.bytes == 3002  # the engine gets the image itself
    with db.session_scope() as s:
        assert "jpeg-bytes" not in str(s.scalars(select(Scan)).one().request_payload)


def test_stated_details_override_identification(client, tenant, identifier, piece_comps):
    body = _scan(client, tenant, photos=photo_url(),
                 metal={"type": "platinum", "purity": "950", "weight_grams": 7.2},
                 brand="Maison Aurelle", signed=True).json()
    sources = body["identification"]["field_sources"]
    assert body["identification"]["source"] == "photos_and_details"
    assert sources["metal"] == sources["brand"] == sources["signed"] == "stated"
    assert sources["stones"] == "identified"
    assert identifier.calls[-1][1]["brand"] == "Maison Aurelle"  # sent as hints
    query = piece_comps.calls[-1][0]
    assert query.metal.weight_grams == 7.2 and query.brand == "maison_aurelle"


def test_stated_loose_stone_drops_identified_setting(client, tenant, identifier, live_engine):
    body = _scan(client, tenant, photos=photo_url(), item_type="loose_stone").json()
    item = body["item"]
    assert item["item_type"] == "loose_stone"
    assert item["metal"] is None
    assert len(item["stones"]) == 1 and item["stones"][0]["role"] == "center"


def test_unidentified_scan_has_no_price(client, tenant, identifier, live_engine):
    identifier.result = Identification(
        status="unidentified", confidence=0.0, summary="Too blurry to identify",
        next_evidence=("Clear, well-lit photos",))
    resp = _scan(client, tenant, photos=photo_url())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "unidentified"
    assert body["price"] is None and body["comps"] == [] and body["item"] is None
    assert body["confidence"]["reasons"][0]["code"] == "unidentified"
    assert body["identification"]["next_evidence"] == ["Clear, well-lit photos"]
    assert live_engine.calls == []  # nothing priced
    usage = client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"][0]
    assert (usage["scans_total"], usage["scans_priced"]) == (1, 0)


def test_low_confidence_identification_is_not_priced(client, tenant, identifier, live_engine):
    identifier.result = Identification(status="identified", item=RING_IDENTIFICATION.item,
                                       confidence=0.3, summary="Possibly a ring")
    body = _scan(client, tenant, photos=photo_url()).json()
    assert body["status"] == "unidentified"
    assert "too uncertain" in body["confidence"]["reasons"][0]["message"]


def test_uncertain_identification_lowers_confidence(client, tenant, identifier, piece_comps):
    identifier.result = Identification(status="identified", item=RING_IDENTIFICATION.item,
                                       confidence=0.6, summary="Probably a platinum ring")
    body = _scan(client, tenant, photos=photo_url()).json()
    assert body["status"] == "priced"
    assert "uncertain_identification" in [r["code"] for r in body["confidence"]["reasons"]]


def test_incomplete_identified_item_is_unidentified(client, tenant, identifier, live_engine):
    identifier.result = Identification(status="identified", item={"item_type": "ring"},
                                       confidence=0.8, summary="A ring")
    body = _scan(client, tenant, photos=photo_url()).json()
    assert body["status"] == "unidentified"
    assert "metal, stones" in body["confidence"]["reasons"][0]["message"]


def test_processing_scan_completes_on_retrieval(client, tenant, identifier, piece_comps, settings):
    # The POST polls 8 times (identify_wait_seconds / poll interval); pending for 9 polls
    # means it is still processing after the POST and finishes on the first GET.
    identifier.pending = 9
    resp = _scan(client, tenant, photos=photo_url())
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "processing"
    assert body["identification"]["status"] == "processing"
    assert body["price"] is None and body["confidence"] is None
    assert f"/scans/{body['scan_id']}" in body["warnings"][0]
    assert client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"] == []  # not metered

    done = client.get(f"{BASE}/scans/{body['scan_id']}", headers=tenant.headers).json()
    assert done["status"] == "priced"
    assert done["scan_id"] == body["scan_id"]
    assert done["identification"]["summary"] == RING_IDENTIFICATION.summary
    again = client.get(f"{BASE}/scans/{body['scan_id']}", headers=tenant.headers).json()
    assert again == done
    usage = client.get(f"{BASE}/usage", headers=tenant.headers).json()["data"][0]
    assert usage["scans_total"] == 1


def test_identification_outage_is_503_and_stores_nothing(client, tenant, identifier, db):
    identifier.fail = True
    resp = _scan(client, tenant, photos=photo_url())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "identification_unavailable"
    with db.session_scope() as s:
        assert s.scalars(select(Scan)).all() == []


def test_live_tenant_without_identifier(db, settings, clock, make_tenant, live_engine):
    app = create_app(settings, db=db, live_engine=live_engine, live_identifier=None, clock=clock)
    tenant = make_tenant()
    with TestClient(app) as c:
        photos = c.post(f"{BASE}/scans", headers=tenant.headers, json={**BASIS, "photos": photo_url()})
        assert photos.status_code == 503
        assert photos.json()["error"]["code"] == "identification_unavailable"
        details = c.post(f"{BASE}/scans", headers=tenant.headers, json={
            **BASIS, "item_type": "loose_stone",
            "stones": [{"variety": "ruby", "carat": 1.2, "shape": "oval"}]})
        assert details.status_code == 201


def test_details_only_scan(client, tenant, identifier, live_engine):
    body = _scan(client, tenant, item_type="loose_stone",
                 stones=[{"variety": "blue sapphire", "carat": 2.0, "shape": "oval",
                          "treatment": "heated", "origin": "Sri Lanka"}]).json()
    ident = body["identification"]
    assert ident["source"] == "details" and ident["confidence"] is None
    assert ident["field_sources"] == {"item_type": "stated", "stones": "stated"}
    assert identifier.calls == []


@pytest.mark.parametrize(
    "photos",
    [
        [{"url": "https://x.example/a.jpg", "data": "aGk=", "media_type": "image/jpeg"}],
        [{"data": "not base64!!", "media_type": "image/jpeg"}],
        [{"data": "aGk="}],
        [{"url": "http://insecure.example/a.jpg"}],
        [{"url": "https://x.example/a.jpg", "media_type": "image/gif"}],
        photo_url(6),
    ],
)
def test_invalid_photos_are_rejected(client, tenant, photos):
    resp = _scan(client, tenant, photos=photos)
    assert resp.status_code == 422, photos
    assert resp.json()["error"]["code"] == "invalid_request"


def test_photos_make_item_fields_optional_but_details_alone_must_be_complete(client, tenant):
    assert _scan(client, tenant, photos=photo_url()).status_code == 201
    assert _scan(client, tenant).status_code == 422  # nothing to identify or price
    assert _scan(client, tenant, item_type="ring").status_code == 422  # a ring needs metal/stones


def test_sandbox_photo_scan_is_simulated_and_deterministic(client, make_tenant):
    sandbox = make_tenant(environment="sandbox")
    photo = [{"data": base64.b64encode(b"\xff\xd8" + b"p" * 4000).decode(), "media_type": "image/jpeg"}]
    first = _scan(client, sandbox, photos=photo).json()
    second = _scan(client, sandbox, photos=photo).json()
    assert first["synthetic_data"] is True
    assert any("simulated" in w for w in first["warnings"])
    assert first["identification"]["summary"] == second["identification"]["summary"]
    assert first["status"] in ("priced", "insufficient_data")

    tiny = [{"data": base64.b64encode(b"tiny").decode(), "media_type": "image/png"}]
    assert _scan(client, sandbox, photos=tiny).json()["status"] == "unidentified"

    ring = _scan(client, sandbox, photos=photo_url(), item_type="ring").json()
    assert ring["identification"]["summary"].startswith("Platinum ring")
    assert ring["status"] == "priced" and ring["breakdown"] is not None


def test_batch_rows_with_photo_urls(client, tenant, identifier, piece_comps):
    text = ("row_id,photo_urls,metal_weight_grams,metal_type\n"
            "P1,https://photos.example.com/a.jpg|https://photos.example.com/b.jpg,,\n"
            "P2,https://photos.example.com/c.jpg,7.5,platinum\n")
    job = client.post(f"{BASE}/scans/batch", headers=tenant.headers,
                      files={"file": ("photos.csv", text, "text/csv")},
                      data={"valuation_basis": "fair_market"}).json()
    rows = list(csv.DictReader(io.StringIO(client.get(
        f"{BASE}/scans/batch/{job['job_id']}/results", headers=tenant.headers).text)))
    assert [r["status"] for r in rows] == ["priced", "priced"]
    assert rows[0]["identified_as"] == RING_IDENTIFICATION.summary
    assert rows[0]["identification_confidence"] == "0.9"
    assert rows[0]["item_type"] == "ring" and rows[0]["scan_id"].startswith("scn_")
    assert len(identifier.calls[0][0]) == 2
    assert identifier.calls[1][1]["metal"]["weight_grams"] == 7.5  # stated columns are hints


def test_merge_item_rules():
    identified = {"item_type": "ring", "metal": {"type": "gold"}, "condition": "good",
                  "stones": [{"role": "side", "variety": "diamond", "carat": 0.5, "count": 10},
                             {"role": "center", "variety": "ruby", "carat": 1.1, "shape": "oval"}],
                  "unexpected": "ignored"}
    merged, sources = merge_item(identified, {"condition": "fair"})
    assert merged["condition"] == "fair" and sources["condition"] == "stated"
    assert "unexpected" not in merged

    loose, loose_sources = merge_item(identified, {"item_type": "loose_stone"})
    assert "metal" not in loose and "condition" not in loose
    assert loose["stones"] == [{"role": "center", "variety": "ruby", "carat": 1.1,
                                "shape": "oval", "count": 1}]
    assert loose_sources == {"item_type": "stated", "stones": "identified"}
