"""
HTTP adapter to the GemstoneAI scan in the backend (not in this repository).

The contract below mirrors what today's scan already returns (the fields of
the GemstoneAI agent tools ``analyze_jewelry``, ``get_jewelry_analysis`` and
``find_jewelry_comparables``). The field names are a proposal: check them
against a real scan result and adjust ``_analysis`` and ``_comparable``,
which are the only places that read the backend's payloads.

    POST {ENTERPRISE_SCAN_URL}/analyses
    { "photos": [ {"url": "https://..."} | {"data": "<base64>", "media_type": "image/jpeg"} ],
      "details": { "item_type": "ring", "metal": {...}, ... },      # optional hints
      "model_version": "..." }                                       # optional pin
    -> 200 { "analysis_id": "ana_...", "status": "completed", "model_version": "...",
             "identification": { "status": "identified" | "unidentified",
                                 "summary": "...", "confidence": 0.86,
                                 "alternatives": [{"label": "...", "probability": 0.08}] },
             "observations": ["..."], "open_questions": ["..."], "next_evidence": ["..."],
             "price_context": { "status": "provided" | "withheld", "currency": "USD",
                                "low": 2400, "high": 6600, "basis": "retail replacement",
                                "explanation": "...", "withheld_reason": null } }
    -> 202 { "analysis_id": "ana_...", "status": "processing" }
    -> 422 { "error": "unsupported_model_version", "available": ["..."] }

    GET {ENTERPRISE_SCAN_URL}/analyses/{analysis_id}              -> the same 200 / 202 shapes
    GET {ENTERPRISE_SCAN_URL}/analyses/{analysis_id}/comparables
    -> 200 { "status": "found" | "none_found",
             "comparables": [ { "source": "...", "title": "...", "url": "https://...",
                                "photo_url": "https://...", "price": 3100, "currency": "USD",
                                "price_type": "asking" | "realized", "observed_at": "2026-08-14",
                                "differences": ["Smaller center stone"] } ] }

Links into the consumer app are deliberately not mapped: enterprise customers
never see consumer branding. Enterprise scans should also not count against,
or appear in, any consumer account; that is the backend endpoint's job.
"""

import base64
import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, Optional

import httpx

from enterprise.engine.base import (
    PRICE_TYPES,
    AnalysisResult,
    Comparable,
    PendingAnalysis,
    PhotoRef,
    PriceContext,
    ScanEngine,
    ScanUnavailable,
    UnsupportedModelVersion,
)

logger = logging.getLogger(__name__)


def _opt_float(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


class RemoteScanEngine(ScanEngine):
    name = "gemstoneai-scan"
    synthetic = False

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 20.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, headers=headers, transport=transport
        )

    # ── calls ────────────────────────────────────────────────────────────────

    def analyze(self, photos: list[PhotoRef], details: Mapping[str, Any],
                model_version: Optional[str]) -> "AnalysisResult | PendingAnalysis":
        body: dict[str, Any] = {"photos": [self._photo(p) for p in photos],
                                "details": dict(details)}
        if model_version:
            body["model_version"] = model_version
        return self._analysis(self._request("POST", "/analyses", json=body))

    def get(self, analysis_id: str) -> "AnalysisResult | PendingAnalysis":
        return self._analysis(self._request("GET", f"/analyses/{analysis_id}"))

    def comparables(self, analysis_id: str) -> list[Comparable]:
        resp = self._request("GET", f"/analyses/{analysis_id}/comparables")
        try:
            data = resp.json()
            items = data.get("comparables") or []
            if not isinstance(items, list):
                raise TypeError("comparables is not a list")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ScanUnavailable("scan returned malformed comparables") from exc
        out = []
        for raw in items:
            comp = self._comparable(raw)
            if comp is not None:
                out.append(comp)
        return out

    # ── mapping (the only code that reads backend payloads) ─────────────────

    @staticmethod
    def _photo(p: PhotoRef) -> dict:
        if p.data is not None:
            return {"data": base64.b64encode(p.data).decode("ascii"), "media_type": p.media_type}
        return {"url": p.url}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ScanUnavailable(f"scan unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 422:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if data.get("error") == "unsupported_model_version":
                requested = (kwargs.get("json") or {}).get("model_version", "")
                raise UnsupportedModelVersion(requested, list(data.get("available") or []))
        if resp.status_code not in (200, 202):
            raise ScanUnavailable(f"scan returned HTTP {resp.status_code}")
        return resp

    @staticmethod
    def _analysis(resp: httpx.Response) -> "AnalysisResult | PendingAnalysis":
        try:
            data = resp.json()
            if resp.status_code == 202 or data.get("status") == "processing":
                return PendingAnalysis(analysis_id=str(data["analysis_id"]))
            ident = data.get("identification") or {}
            status = ident.get("status")
            if status not in ("identified", "unidentified"):
                raise ValueError(f"unknown identification status {status!r}")
            price = data.get("price_context") or {}
            price_status = price.get("status", "withheld")
            if price_status not in ("provided", "withheld"):
                raise ValueError(f"unknown price status {price_status!r}")
            return AnalysisResult(
                analysis_id=str(data["analysis_id"]),
                model_version=str(data.get("model_version") or "unversioned"),
                identified=status == "identified",
                summary=ident.get("summary"),
                confidence=_opt_float(ident.get("confidence")),
                observations=tuple(str(o) for o in data.get("observations") or ()),
                alternatives=tuple(
                    {"label": str(a["label"]), "probability": float(a["probability"])}
                    for a in ident.get("alternatives") or ()
                ),
                open_questions=tuple(str(q) for q in data.get("open_questions") or ()),
                next_evidence=tuple(str(e) for e in data.get("next_evidence") or ()),
                price_context=PriceContext(
                    status=price_status,
                    currency=str(price.get("currency") or "USD"),
                    low=_opt_float(price.get("low")),
                    high=_opt_float(price.get("high")),
                    basis=price.get("basis"),
                    explanation=price.get("explanation"),
                    withheld_reason=price.get("withheld_reason"),
                ),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ScanUnavailable("scan returned a malformed analysis") from exc

    @staticmethod
    def _comparable(raw: Any) -> Optional[Comparable]:
        try:
            price_type = str(raw.get("price_type") or "")
            if price_type not in PRICE_TYPES:
                # Untagged comparables are never shown as evidence.
                raise ValueError(f"price_type {price_type!r}")
            observed = raw.get("observed_at")
            return Comparable(
                source=str(raw.get("source") or "unknown"),
                price=float(raw["price"]),
                currency=str(raw.get("currency") or "USD"),
                price_type=price_type,
                observed_on=date.fromisoformat(str(observed)[:10]) if observed else None,
                title=raw.get("title"),
                url=raw.get("url"),
                photo_url=raw.get("photo_url"),
                differences=tuple(str(d) for d in raw.get("differences") or ()),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning("dropping malformed or untagged comparable: %r", raw)
            return None

    def close(self) -> None:
        self._client.close()
