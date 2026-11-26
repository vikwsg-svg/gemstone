"""Shared helpers for enterprise tests."""

import httpx

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from enterprise.engine.base import (
    AnalysisResult,
    Comparable,
    PendingAnalysis,
    PriceContext,
    ScanEngine,
    ScanUnavailable,
    UnsupportedModelVersion,
)

BASE = "/enterprise/v1"
FIXED_NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = FIXED_NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def comp(i: int, *, price: float = 5000.0, price_type: str = "asking") -> Comparable:
    return Comparable(source=f"dealer-{i}", price=price, currency="USD", price_type=price_type,
                      observed_on=date(2026, 10, 1), title=f"Comparable ring {i}",
                      url=f"https://market.example/{i}", differences=("Smaller center stone",))


RING = AnalysisResult(
    analysis_id="ana_ring",
    model_version="scan-v7",
    identified=True,
    summary="Platinum ring with a round diamond of about 1 ct",
    confidence=0.9,
    observations=("Round brilliant center stone", "PT950 stamp inside the band"),
    alternatives=({"label": "Center stone is moissanite", "probability": 0.05},),
    open_questions=("How much does the ring weigh?",),
    next_evidence=("Weigh the ring in grams",),
    price_context=PriceContext(status="provided", low=4000.4, high=6500.6,
                               basis="retail replacement", explanation="From 8 comparable rings."),
)


class FakeScanEngine(ScanEngine):
    """A controllable scan: fixed result and comparables; can stay pending, fail or reject pins."""

    name = "fake-scan"
    synthetic = False

    def __init__(self, result: AnalysisResult = RING, comps: Optional[list] = None,
                 pending: int = 0, fail: bool = False, fail_comparables: bool = False,
                 versions: tuple = ("scan-v6", "scan-v7")):
        self.result = result
        self.comps = [comp(i) for i in range(8)] if comps is None else comps
        self.pending = pending
        self.fail = fail
        self.fail_comparables = fail_comparables
        self.versions = versions
        self.analyze_calls: list = []
        self.comparables_calls: list[str] = []
        self.polls = 0

    def analyze(self, photos, details, model_version):
        self.analyze_calls.append((list(photos), dict(details), model_version))
        if self.fail:
            raise ScanUnavailable("down")
        if model_version is not None and model_version not in self.versions:
            raise UnsupportedModelVersion(model_version, list(self.versions))
        if self.pending > 0:
            return PendingAnalysis(analysis_id=self.result.analysis_id)
        return self._versioned(model_version)

    def get(self, analysis_id):
        self.polls += 1
        if self.fail:
            raise ScanUnavailable("down")
        self.pending -= 1
        if self.pending > 0:
            return PendingAnalysis(analysis_id=analysis_id)
        return self._versioned(None)

    def comparables(self, analysis_id):
        self.comparables_calls.append(analysis_id)
        if self.fail_comparables:
            raise ScanUnavailable("down")
        return list(self.comps)

    def _versioned(self, model_version):
        return replace(self.result, model_version=model_version) if model_version else self.result


@dataclass
class TenantKey:
    tenant_id: str
    key: str
    key_id: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}


def photo_urls(n: int = 1) -> list[dict]:
    return [{"url": f"https://photos.example.com/item-{i}.jpg"} for i in range(n)]


def scan_body(**overrides) -> dict:
    body = {"photos": photo_urls(1)}
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


class HookRecorder:
    """A webhook receiver: records each delivery and answers with ``status``."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def events(self) -> list[str]:
        return [r.headers["GemstoneAI-Event-Type"] for r in self.requests]

