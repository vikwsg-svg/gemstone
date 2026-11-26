"""Index: published GPI levels served as a feed; never overwritten; sandbox examples."""

import json

import pytest
from click.testing import CliRunner
from sqlalchemy import func, select

from enterprise.cli import cli
from enterprise.index import IndexPublishError, change_pct, correct, publish, shift_period
from enterprise.models import IndexCorrection, IndexSnapshot
from enterprise.settings import get_settings
from tests.enterprise.helpers import BASE, FIXED_NOW


def row(period, level, comps=900, key="gpi", methodology="gpi-1.0"):
    return {"series_key": key, "period": period, "level": level, "comp_count": comps,
            "methodology_version": methodology}


@pytest.fixture
def key(make_tenant):
    return make_tenant(scopes=("index",))


@pytest.fixture
def published(db):
    with db.session_scope() as s:
        publish(s, [row("2025-09", 100), row("2025-10", 101), row("2026-09", 104, comps=20),
                    row("2026-10", 105.5)], FIXED_NOW)
        s.commit()


def test_change_and_period_helpers():
    assert change_pct(105.5, 101) == 4.46
    assert change_pct(100, None) is None and change_pct(100, 0) is None
    assert shift_period("2026-01", -1) == "2025-12" and shift_period("2025-12", 12) == "2026-12"


def test_series_values_changes_and_thin_flag(client, key, published):
    body = client.get(f"{BASE}/index/series/gpi", headers=key.headers).json()
    assert body["synthetic_data"] is False and body["methodology_version"] == "gpi-1.0"
    by_period = {p["period"]: p for p in body["data"]}
    assert by_period["2025-10"]["change_mom_pct"] == 1.0
    assert by_period["2026-10"]["change_mom_pct"] == pytest.approx(1.44)
    assert by_period["2026-10"]["change_yoy_pct"] == pytest.approx(4.46)
    assert by_period["2025-09"]["change_mom_pct"] is None  # no previous month published
    assert by_period["2026-09"]["thin"] is True and by_period["2026-10"]["thin"] is False
    assert by_period["2026-10"]["published_at"].endswith("Z")


def test_window_keeps_changes_from_outside_it(client, key, published):
    data = client.get(f"{BASE}/index/series/gpi?start=2026-10", headers=key.headers).json()["data"]
    assert [p["period"] for p in data] == ["2026-10"]
    assert data[0]["change_yoy_pct"] == pytest.approx(4.46)


def test_series_csv(client, key, published):
    r = client.get(f"{BASE}/index/series/gpi?format=csv&start=2026-09", headers=key.headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == "period,level,comp_count,thin,change_mom_pct,change_yoy_pct,revised,published_at"
    assert lines[1].startswith("2026-09,104.0,20,true,,")


def test_series_list(client, key, published):
    data = client.get(f"{BASE}/index/series", headers=key.headers).json()["data"]
    gpi = next(s for s in data if s["key"] == "gpi")
    assert gpi["first_period"] == "2025-09" and gpi["latest"]["period"] == "2026-10"
    unpublished = next(s for s in data if s["key"] == "period-jewelry")
    assert unpublished["latest"] is None and unpublished["first_period"] is None


def test_publishing_never_overwrites(db):
    with db.session_scope() as s:
        assert publish(s, [row("2026-09", 104)], FIXED_NOW) == {"published": 1, "unchanged": 0}
        s.commit()
        assert publish(s, [row("2026-09", 104)], FIXED_NOW) == {"published": 0, "unchanged": 1}
        with pytest.raises(IndexPublishError, match="never overwritten"):
            publish(s, [row("2026-09", 104.2)], FIXED_NOW)


@pytest.mark.parametrize("bad,message", [
    (row("2026-11", 100), "not complete"),
    (row("2026-13", 100), "YYYY-MM"),
    (row("2026-08", 0), "positive"),
    (row("2026-08", 100, key="watches"), "unknown series"),
    (row("2026-08", 100, methodology=""), "methodology_version"),
])
def test_publish_is_all_or_nothing(db, bad, message):
    with db.session_scope() as s:
        with pytest.raises(IndexPublishError, match=message):
            publish(s, [row("2026-07", 100), bad], FIXED_NOW)
        s.rollback()
        assert s.scalar(select(func.count()).select_from(IndexSnapshot)) == 0


def test_corrections_keep_the_original_and_are_listed(client, key, db, published):
    with db.session_scope() as s:
        correct(s, "gpi", "2026-09", 104.6, "Late auction results added", FIXED_NOW)
        s.commit()
        assert s.scalar(select(func.count()).select_from(IndexSnapshot)
                        .where(IndexSnapshot.period == "2026-09")) == 2  # original kept
        assert s.scalar(select(func.count()).select_from(IndexCorrection)) == 1
        with pytest.raises(IndexPublishError, match="has not been published"):
            correct(s, "gpi", "2024-01", 99, "x", FIXED_NOW)

    data = client.get(f"{BASE}/index/series/gpi", headers=key.headers).json()["data"]
    point = next(p for p in data if p["period"] == "2026-09")
    assert point["level"] == 104.6 and point["revised"] is True and point["comp_count"] == 20

    corrections = client.get(f"{BASE}/index/corrections?series=gpi", headers=key.headers).json()
    assert corrections["data"] == [{
        "series_key": "gpi", "period": "2026-09", "old_level": 104.0, "new_level": 104.6,
        "change_pct": 0.58, "reason": "Late auction results added",
        "published_at": "2026-11-24T12:00:00Z"}]
    assert client.get(f"{BASE}/index/corrections?since=2026-12-01",
                      headers=key.headers).json()["data"] == []


def test_validation_and_scope(client, key, make_tenant):
    assert client.get(f"{BASE}/index/series/watches", headers=key.headers).status_code == 404
    assert client.get(f"{BASE}/index/series/gpi?start=2026-13", headers=key.headers).status_code == 422
    assert client.get(f"{BASE}/index/series/gpi?start=2026-10&end=2026-01",
                      headers=key.headers).status_code == 422
    scan_only = make_tenant("Scan only", scopes=("scan",))
    assert client.get(f"{BASE}/index/series", headers=scan_only.headers).status_code == 403


def test_sandbox_series_are_examples_and_consistent(client, make_tenant):
    sandbox = make_tenant("Sandbox", environment="sandbox", scopes=("index",))
    listing = client.get(f"{BASE}/index/series", headers=sandbox.headers).json()
    assert listing["synthetic_data"] is True
    assert all(s["latest"]["period"] == "2026-10" for s in listing["data"])
    gpi = client.get(f"{BASE}/index/series/gpi", headers=sandbox.headers).json()
    assert gpi["data"][0]["period"] == "2023-01" and gpi["methodology_version"] == "sandbox-example"
    # The headline is built from the sub-indices: its comparable count is their sum.
    subs = [s for s in listing["data"] if s["key"] != "gpi"]
    headline = next(s for s in listing["data"] if s["key"] == "gpi")
    assert headline["latest"]["comp_count"] == sum(s["latest"]["comp_count"] for s in subs)
    period = client.get(f"{BASE}/index/series/period-jewelry", headers=sandbox.headers).json()
    assert any(p["thin"] for p in period["data"])  # thin months are shown, not hidden
    # Deterministic: the same request returns the same numbers.
    assert client.get(f"{BASE}/index/series/gpi", headers=sandbox.headers).json() == gpi


def test_operator_cli(db, tmp_path):
    feed = tmp_path / "gpi.csv"
    feed.write_text("series_key,period,level,comp_count,methodology_version\n"
                    "gpi,2026-09,104.0,880,gpi-1.0\ndiamonds-natural,2026-09,98.5,310,gpi-1.0\n")

    def run(*args):
        get_settings.cache_clear()
        result = CliRunner().invoke(cli, list(args), env={"ENTERPRISE_DATABASE_URL": db.url})
        get_settings.cache_clear()
        return result

    ok = run("index-publish", "--file", str(feed))
    assert ok.exit_code == 0, ok.output and json.loads(ok.stdout) == {"published": 2, "unchanged": 0}
    fixed = run("index-correct", "--series", "gpi", "--period", "2026-09", "--level", "104.3",
                "--reason", "Late results")
    assert fixed.exit_code == 0, fixed.output
    assert json.loads(fixed.stdout)["old_level"] == 104.0
    refused = run("index-correct", "--series", "gpi", "--period", "2020-01", "--level", "1",
                  "--reason", "x")
    assert refused.exit_code != 0 and "has not been published" in refused.output
