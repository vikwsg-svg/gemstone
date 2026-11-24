"""
HTTP adapter to the production V2 comps engine.

The V2 engine lives in the GemstoneAI backend, which is not in this
repository. This adapter defines the contract the enterprise layer needs
from it; wiring it up means exposing (or mapping to) one internal endpoint:

    POST {ENTERPRISE_ENGINE_URL}/comps/search
    Authorization: Bearer {ENTERPRISE_ENGINE_API_KEY}
    {
      "query": {
        "item_type": "ring",              # or loose_stone, earrings, necklace, pendant, ...
        "stones": [
          { "role": "center", "variety": "diamond", "species": "diamond", "carat": 1.52,
            "count": 1, "shape": "round", "color_grade": "g", "clarity": "vs1",
            "cut_grade": "excellent", "treatment": null, "origin": null,
            "growth": "natural", "lab": "GIA", "lab_report_number": "...",
            "length_mm": null, "width_mm": null, "depth_mm": null },
          { "role": "side", "variety": "diamond", "carat": 0.4, "count": 12, ... }
        ],
        "metal": { "type": "platinum", "purity": "950", "color": "white", "weight_grams": 5.8 },
        "brand": null, "signed": null, "era": "contemporary", "condition": "excellent",
        "total_weight_grams": null
      },
      "engine_version": "v2.4",
      "limit": 60
    }
    -> 200 {
      "comps": [
        { "comp_id": "lst_123", "source_type": "asking",          # or "realized"
          "source": "dealer:leibish", "observed_on": "2026-08-14",
          "currency": "USD", "price_total": 8450.0,
          "carat": 1.48,                            # loose stones; optional for pieces
          "similarity": 0.91,
          "adjusted_price_per_carat": 4210.5,       # optional, loose stones
          "adjusted_price_total": 8120.0,           # optional, pieces
          "attributes": { "item_type": "ring", "metal_type": "platinum", ... } }
      ],
      "breakdown_shares": { "raw": 0.55, "make": 0.18, "market": 0.27 }   # optional, pieces
    }

Comps without a valid ``source_type`` are rejected by the valuation service
(and reported in ``comps_summary.rejected``), never silently priced.
"""

import logging
from datetime import date
from typing import Any, Optional

import httpx

from enterprise.engine.base import (
    BREAKDOWN_PARTS,
    CompsEngine,
    EngineComp,
    EngineResult,
    EngineUnavailable,
    ItemQuery,
)

logger = logging.getLogger(__name__)


class RemoteCompsEngine(CompsEngine):
    name = "v2"
    synthetic = False

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, headers=headers, transport=transport
        )

    def find_comps(self, query: ItemQuery, *, engine_version: str, limit: int) -> EngineResult:
        try:
            resp = self._client.post(
                "/comps/search",
                json={"query": query.to_dict(), "engine_version": engine_version, "limit": limit},
            )
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"comps engine unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise EngineUnavailable(f"comps engine returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
            raw_comps = payload["comps"]
            if not isinstance(raw_comps, list):
                raise TypeError("comps is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            raise EngineUnavailable("comps engine returned a malformed response") from exc

        comps: list[EngineComp] = []
        for raw in raw_comps:
            comp = self._parse(raw)
            if comp is not None:
                comps.append(comp)
        return EngineResult(comps=comps, breakdown_shares=self._shares(payload))

    @staticmethod
    def _shares(payload: Any) -> Optional[dict[str, float]]:
        raw = payload.get("breakdown_shares") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            shares = {k: float(raw[k]) for k in BREAKDOWN_PARTS}
        except (KeyError, TypeError, ValueError):
            logger.warning("ignoring malformed breakdown_shares from engine: %r", raw)
            return None
        if any(v < 0 for v in shares.values()) or abs(sum(shares.values()) - 1.0) > 0.01:
            logger.warning("ignoring breakdown_shares that do not sum to 1: %r", raw)
            return None
        return shares

    @staticmethod
    def _parse(raw: Any) -> Optional[EngineComp]:
        def _opt(key: str) -> Optional[float]:
            value = raw.get(key)
            return float(value) if value is not None else None

        try:
            return EngineComp(
                comp_id=str(raw["comp_id"]),
                source_type=str(raw.get("source_type") or ""),
                source=str(raw.get("source") or "unknown"),
                observed_on=date.fromisoformat(str(raw["observed_on"])[:10]),
                currency=str(raw.get("currency") or "USD"),
                price_total=float(raw["price_total"]),
                carat=_opt("carat"),
                similarity=float(raw["similarity"]),
                attributes=dict(raw.get("attributes") or {}),
                adjusted_price_per_carat=_opt("adjusted_price_per_carat"),
                adjusted_price_total=_opt("adjusted_price_total"),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning("dropping malformed comp from engine: %r", raw)
            return None

    def close(self) -> None:
        self._client.close()
