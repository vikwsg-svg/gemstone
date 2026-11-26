"""Intake and Verify: the scan plus one layer of rules each."""

from dataclasses import replace

import pytest

from enterprise.engine.base import PriceContext
from tests.enterprise.helpers import BASE, RING, comp, photo_urls

ALL = ("scan", "intake", "verify")


def priced(low: float, high: float, **kw):
    return replace(RING, price_context=PriceContext(status="provided", low=low, high=high,
                                                    basis="retail replacement",
                                                    explanation="From comparables."), **kw)


@pytest.fixture
def key(make_tenant):
    return make_tenant(scopes=ALL)


# ── Intake ───────────────────────────────────────────────────────────────────


def intake(client, key, **body):
    payload = {"photos": photo_urls(1), "reference": "C-1", **body}
    return client.post(f"{BASE}/intake/items", headers=key.headers, json=payload)


@pytest.mark.parametrize("result,decision,code", [
    (priced(300, 900), "list", "clear_to_list"),
    (priced(4000, 6500), "review", "value_above_list"),
    (priced(6000, 9000), "lab", "above_lab_threshold"),
    (priced(10, 40), "decline", "below_floor"),
    (replace(RING, identified=False, confidence=None), "review", "not_identified"),
    (priced(300, 900, confidence=0.6), "review", "confidence_below_list"),
])
def test_intake_routing(client, key, engine, result, decision, code):
    engine.result = result
    r = intake(client, key)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["object"] == "intake_item" and body["id"].startswith("int_")
    assert body["routing"]["decision"] == decision
    assert code in [x["code"] for x in body["routing"]["reasons"]]
    assert body["scan"]["scan_id"].startswith("scn_")  # the evidence travels with the decision
    assert body["routing"]["rules_applied"]["list_max_value"] == 2000


def test_intake_without_a_price_goes_to_review(client, key, engine):
    engine.comps = [comp(i) for i in range(3)]  # under the 5-comparable rule
    body = intake(client, key).json()
    assert body["scan"]["price_context"]["status"] == "withheld"
    assert body["routing"]["decision"] == "review"
    assert body["routing"]["reasons"][0]["code"] == "no_price"


def test_intake_sends_lab_when_a_report_would_settle_a_wide_range(client, key, engine):
    engine.result = priced(1000, 3500, next_evidence=("Get a lab report for the center stone",))
    routing = intake(client, key).json()["routing"]
    assert routing["decision"] == "lab"
    assert routing["reasons"][0]["code"] == "lab_settles_value"


def test_intake_rules_are_per_account_and_validated(client, key, make_tenant, engine):
    other = make_tenant("Other", scopes=ALL)
    r = client.put(f"{BASE}/intake/rules", headers=key.headers,
                   json={"list_max_value": 10000, "lab_min_value": 20000})
    assert r.status_code == 200 and r.json()["list_max_value"] == 10000
    assert client.get(f"{BASE}/intake/rules", headers=key.headers).json()["lab_min_value"] == 20000
    assert client.get(f"{BASE}/intake/rules", headers=other.headers).json()["list_max_value"] == 2000

    assert intake(client, key).json()["routing"]["decision"] == "list"  # 6,500 now under the limit
    assert intake(client, other).json()["routing"]["decision"] == "review"

    bad = client.put(f"{BASE}/intake/rules", headers=key.headers,
                     json={"decline_below_value": 9000, "lab_min_value": 5000})
    assert bad.status_code == 422


def test_intake_passes_the_consignor_description_to_the_scan(client, key, engine):
    body = intake(client, key, consignor_description="My grandmother's platinum ring").json()
    _, details, _ = engine.analyze_calls[0]
    assert "grandmother's platinum ring" in details["notes"]
    assert body["routing"]["consignor_description"] == "My grandmother's platinum ring"
    assert body["routing"]["prefill"]["title"]["value"].startswith("Platinum ring")


def test_intake_record_is_retrievable_isolated_and_metered(client, key, make_tenant):
    body = intake(client, key).json()
    got = client.get(f"{BASE}/intake/items/{body['id']}", headers=key.headers)
    assert got.status_code == 200 and got.json() == body

    other = make_tenant("Other", scopes=ALL)
    assert client.get(f"{BASE}/intake/items/{body['id']}", headers=other.headers).status_code == 404
    # The id is not a Verify check either.
    assert client.get(f"{BASE}/verify/checks/{body['id']}", headers=key.headers).status_code == 404

    usage = client.get(f"{BASE}/usage", headers=key.headers).json()["data"]
    assert [(u["product"], u["scans_total"]) for u in usage] == [("intake", 1)]


def test_products_need_their_own_scope(client, make_tenant):
    scan_only = make_tenant(scopes=("scan",))
    r = intake(client, scan_only)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required_scope"] == "intake"
    assert client.get(f"{BASE}/verify/settings", headers=scan_only.headers).status_code == 403


def test_processing_scan_completes_on_retrieval_and_is_metered_once(client, key, engine):
    engine.pending = 9  # still running after the short wait
    r = intake(client, key)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "processing" and body["routing"] == {"status": "processing"}
    assert client.get(f"{BASE}/usage", headers=key.headers).json()["data"] == []

    engine.pending = 1
    done = client.get(f"{BASE}/intake/items/{body['id']}", headers=key.headers).json()
    assert done["status"] == "completed" and done["routing"]["decision"] == "review"
    again = client.get(f"{BASE}/intake/items/{body['id']}", headers=key.headers).json()
    assert again == done
    usage = client.get(f"{BASE}/usage", headers=key.headers).json()["data"]
    assert [(u["product"], u["scans_total"]) for u in usage] == [("intake", 1)]


def test_product_posts_are_idempotent(client, key, engine):
    headers = {**key.headers, "Idempotency-Key": "cons-112"}
    payload = {"photos": photo_urls(1), "reference": "C-112"}
    first = client.post(f"{BASE}/intake/items", headers=headers, json=payload)
    second = client.post(f"{BASE}/intake/items", headers=headers, json=payload)
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert second.json() == first.json()
    assert len(engine.analyze_calls) == 1


def test_sandbox_keys_get_example_results(client, make_tenant):
    sandbox = make_tenant("Sandbox", environment="sandbox", scopes=ALL)
    body = intake(client, sandbox).json()
    assert body["scan"]["synthetic_data"] is True
    assert body["routing"]["decision"] in {"list", "review", "lab", "decline"}


# ── Verify ───────────────────────────────────────────────────────────────────


def verify(client, key, photos=None, **subject):
    payload = {"photos": photos or photo_urls(1),
               "subject": {"type": "listing", "external_id": "L-1", **subject}}
    return client.post(f"{BASE}/verify/checks", headers=key.headers, json=payload)


def fired(body):
    return {c["code"] for c in body["risk"]["checks"] if c["fired"]}


def test_verify_clean_listing_is_allowed(client, key):
    body = verify(client, key, stated_value=5000).json()
    assert body["object"] == "verify_check" and body["id"].startswith("ver_")
    assert body["risk"]["risk_score"] == 0 and body["risk"]["action"] == "allow"
    assert "not an authentication" in body["risk"]["note"]


def test_verify_price_far_below_the_scan(client, key):
    risk = verify(client, key, stated_value=900).json()["risk"]
    check = next(c for c in risk["checks"] if c["code"] == "price_too_low")
    assert check["fired"] and check["weight"] == 40
    assert check["evidence"] == {"stated_value": 900.0, "scan_low": 4000, "scan_high": 6501,
                                 "currency": "USD"}
    assert risk["action"] == "manual_review"


def test_verify_price_far_above_the_scan(client, key):
    assert fired(verify(client, key, stated_value=20000).json()) == {"price_too_high"}


def test_verify_price_checks_need_a_comparable_price(client, key, engine):
    no_value = verify(client, key).json()["risk"]["checks"]
    assert not next(c for c in no_value if c["code"] == "price_too_low")["ran"]
    other_currency = verify(client, key, stated_value=100, currency="EUR").json()["risk"]["checks"]
    check = next(c for c in other_currency if c["code"] == "price_too_low")
    assert not check["ran"] and "USD" in check["message"]


def test_verify_uncertain_identification_asks_for_a_lab_report(client, key, engine):
    engine.result = replace(RING, confidence=0.5,
                            alternatives=({"label": "Lab-grown diamond", "probability": 0.3},))
    body = verify(client, key).json()
    assert fired(body) == {"identification_uncertain", "possible_other_identity"}
    assert body["risk"]["risk_score"] == 35 and body["risk"]["action"] == "request_lab_report"


def test_verify_finds_reused_photos_within_the_account_only(client, key, make_tenant):
    photos = [{"url": "https://photos.example.com/stolen.jpg"}]
    other = make_tenant("Other", scopes=ALL)
    verify(client, other, photos=photos, external_id="X-1")  # another account: never matched
    first = verify(client, key, photos=photos, external_id="L-1").json()
    assert "duplicate_imagery" not in fired(first)
    again_same_listing = verify(client, key, photos=photos, external_id="L-1").json()
    assert "duplicate_imagery" not in fired(again_same_listing)

    second = verify(client, key, photos=photos, external_id="L-2").json()
    check = next(c for c in second["risk"]["checks"] if c["code"] == "duplicate_imagery")
    assert check["fired"]
    assert {m["reference"] for m in check["evidence"]["matches"]} == {"L-1"}


def test_verify_seller_pattern(client, key):
    def own(i):
        return [{"url": f"https://photos.example.com/listing-{i}.jpg"}]

    for i in range(2):  # two flagged checks for the same seller
        assert verify(client, key, photos=own(i), external_id=f"L-{i}", seller_id="s-9",
                      stated_value=900).json()["risk"]["risk_score"] >= 40
    third = verify(client, key, photos=own(3), external_id="L-3", seller_id="s-9",
                   stated_value=5000).json()
    assert fired(third) == {"seller_pattern"}
    other_seller = verify(client, key, photos=own(4), external_id="L-4", seller_id="s-1",
                          stated_value=5000).json()
    assert fired(other_seller) == set()


def test_verify_hold_threshold_and_settings(client, key):
    body = verify(client, key, stated_value=900,
                  photos=[{"url": "https://photos.example.com/a.jpg"}], external_id="A").json()
    assert body["risk"]["action"] == "manual_review"
    r = client.put(f"{BASE}/verify/settings", headers=key.headers, json={"review_at": 20, "hold_at": 40})
    assert r.status_code == 200
    held = verify(client, key, stated_value=900, external_id="B").json()["risk"]
    assert held["action"] == "hold" and held["settings_applied"]["hold_at"] == 40

    bad = client.put(f"{BASE}/verify/settings", headers=key.headers, json={"review_at": 80, "hold_at": 70})
    assert bad.status_code == 422


def test_verify_requires_a_subject(client, key):
    r = client.post(f"{BASE}/verify/checks", headers=key.headers, json={"photos": photo_urls(1)})
    assert r.status_code == 422


def test_verify_lists_each_earlier_record_once(client, key):
    photos = [{"url": "https://photos.example.com/front.jpg"}, {"url": "https://photos.example.com/back.jpg"}]
    verify(client, key, photos=photos, external_id="L-1")
    check = next(c for c in verify(client, key, photos=photos, external_id="L-2").json()["risk"]["checks"]
                 if c["code"] == "duplicate_imagery")
    assert [m["reference"] for m in check["evidence"]["matches"]] == ["L-1"]
    assert "on 1 other record" in check["message"]
    assert check["evidence"]["matches"][0]["seen_at"].endswith("+00:00")


def test_messages_read_cleanly_when_the_scan_has_no_price(client, key, engine):
    engine.comps = [comp(i) for i in range(3)]
    routing = intake(client, key).json()["routing"]
    assert routing["reasons"][0]["message"].startswith("The scan gave no price. Fewer than 5")
    checks = verify(client, key, stated_value=100).json()["risk"]["checks"]
    low = next(c for c in checks if c["code"] == "price_too_low")
    assert low["message"] == "Not run: the scan gave no price."
    assert low["evidence"]["withheld_reason"].startswith("Fewer than 5")
