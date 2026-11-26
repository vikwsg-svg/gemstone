"""The adapter to the backend's scan, and app wiring."""

import json

import httpx
import pytest

from enterprise.app import live_engine_from_settings
from enterprise.engine.base import (
    AnalysisResult,
    PendingAnalysis,
    PhotoRef,
    ScanUnavailable,
    UnsupportedModelVersion,
)
from enterprise.engine.remote import RemoteScanEngine
from enterprise.engine.sandbox import SandboxScanEngine

ANALYSIS = {
    "analysis_id": "ana_123",
    "status": "completed",
    "model_version": "scan-v7",
    "identification": {"status": "identified", "summary": "18k gold ring", "confidence": 0.82,
                       "alternatives": [{"label": "14k gold", "probability": 0.1}]},
    "observations": ["Yellow metal", "750 stamp"],
    "open_questions": ["Weight?"],
    "next_evidence": ["Weigh it"],
    "price_context": {"status": "provided", "currency": "USD", "low": 900, "high": 1400,
                      "basis": "resale", "explanation": "Mostly gold value."},
    "gemstoneai_url": "https://app.gemstoneai.example/analysis/ana_123",
}
PHOTOS = [PhotoRef(sha256="a" * 64, media_type="image/jpeg", bytes=3, data=b"abc"),
          PhotoRef(sha256="b" * 64, url="https://p.example/x.jpg")]


def _engine(handler):
    return RemoteScanEngine("https://scan.internal", "k", transport=httpx.MockTransport(handler))


def test_analyze_sends_photos_details_and_pin_and_maps_the_result():
    seen = {}

    def handler(request):
        seen["path"], seen["auth"] = request.url.path, request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ANALYSIS)

    result = _engine(handler).analyze(PHOTOS, {"item_type": "ring"}, "scan-v7")
    assert seen["path"] == "/analyses" and seen["auth"] == "Bearer k"
    assert seen["body"] == {"photos": [{"data": "YWJj", "media_type": "image/jpeg"},
                                       {"url": "https://p.example/x.jpg"}],
                            "details": {"item_type": "ring"}, "model_version": "scan-v7"}
    assert isinstance(result, AnalysisResult)
    assert result.identified and result.confidence == 0.82 and result.model_version == "scan-v7"
    assert result.alternatives == ({"label": "14k gold", "probability": 0.1},)
    assert result.price_context.low == 900 and result.price_context.basis == "resale"
    # Consumer-app links are not carried into enterprise results.
    assert "gemstoneai" not in repr(result)


def test_pending_then_get():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"analysis_id": "ana_9", "status": "processing"})
        assert request.url.path == "/analyses/ana_9"
        return httpx.Response(200, json=ANALYSIS)

    engine = _engine(handler)
    assert engine.analyze(PHOTOS, {}, None) == PendingAnalysis(analysis_id="ana_9")
    assert engine.get("ana_9").summary == "18k gold ring"


def test_comparables_are_mapped_and_untagged_ones_dropped():
    def handler(request):
        assert request.url.path == "/analyses/ana_123/comparables"
        return httpx.Response(200, json={"status": "found", "comparables": [
            {"source": "market", "title": "Gold ring", "url": "https://m.example/1", "price": 1100,
             "currency": "USD", "price_type": "realized", "observed_at": "2026-08-14T10:00:00Z",
             "differences": ["Heavier"]},
            {"source": "dealer", "price": 1500, "price_type": "retail"},   # untagged: dropped
            {"source": "broken"},                                          # malformed: dropped
        ]})

    comps = _engine(handler).comparables("ana_123")
    assert len(comps) == 1
    assert comps[0].price_type == "realized" and comps[0].observed_on.isoformat() == "2026-08-14"
    assert comps[0].differences == ("Heavier",)


def test_none_found_is_an_empty_list():
    def handler(request):
        return httpx.Response(200, json={"status": "none_found", "comparables": []})

    assert _engine(handler).comparables("ana_1") == []


def test_unsupported_model_version():
    def handler(request):
        return httpx.Response(422, json={"error": "unsupported_model_version",
                                         "available": ["scan-v7"]})

    with pytest.raises(UnsupportedModelVersion) as exc:
        _engine(handler).analyze(PHOTOS, {}, "scan-v1")
    assert exc.value.requested == "scan-v1" and exc.value.available == ["scan-v7"]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(422, json={"error": "other"}),
     httpx.Response(200, json={"status": "completed", "analysis_id": "a",
                               "identification": {"status": "weird"}}),
     httpx.Response(200, json={"status": "completed"}), httpx.Response(200, content=b"x")],
)
def test_failures_raise_unavailable(response):
    with pytest.raises(ScanUnavailable):
        _engine(lambda request: response).analyze(PHOTOS, {}, None)


def test_network_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ScanUnavailable):
        _engine(handler).comparables("ana_1")


def test_live_engine_from_settings(settings):
    assert live_engine_from_settings(settings) is None
    remote = settings.model_copy(update={"scan_backend": "remote", "scan_url": "http://x"})
    assert isinstance(live_engine_from_settings(remote), RemoteScanEngine)
    with pytest.raises(RuntimeError):
        live_engine_from_settings(settings.model_copy(update={"scan_backend": "remote"}))
    sandbox = settings.model_copy(update={"scan_backend": "sandbox"})
    assert isinstance(live_engine_from_settings(sandbox), SandboxScanEngine)
