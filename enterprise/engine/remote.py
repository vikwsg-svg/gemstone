"""
HTTP adapter to the production V2 comps engine.

The V2 engine lives in the GemstoneAI backend, which is not in this
repository. This adapter defines the contract the enterprise layer needs
from it; wiring it up means exposing (or mapping to) one internal endpoint:

    POST {ENTERPRISE_ENGINE_URL}/comps/search
    Authorization: Bearer {ENTERPRISE_ENGINE_API_KEY}
    {
      "query": { "variety": "blue_sapphire", "species": "corundum", "carat": 2.1,
                 "shape": "oval", "color_grade": null, "clarity": null,
                 "treatment": "heated", "origin": null, "lab": "GIA",
                 "lab_report_number": "...", "length_mm": null, "width_mm": null,
                 "depth_mm": null },
      "engine_version": "v2.4",
      "limit": 60
    }
    -> 200 {
      "comps": [
        { "comp_id": "lst_123", "source_type": "asking",          # or "realized"
          "source": "dealer:leibish", "observed_on": "2026-08-14",
          "currency": "USD", "price_total": 8450.0, "carat": 2.05,
          "similarity": 0.91, "adjusted_price_per_carat": 4210.5,  # optional
          "attributes": { "variety": "blue_sapphire", "origin": "sri_lanka", ... } }
      ]
    }

Comps without a valid ``source_type`` are rejected by the valuation service
(and reported in ``comps_summary.rejected``), never silently priced.
"""

import logging
from datetime import date
from typing import Any, Optional

import httpx

from enterprise.engine.base import CompsEngine, EngineComp, EngineUnavailable, StoneQuery

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

    def find_comps(
        self, query: StoneQuery, *, engine_version: str, limit: int
    ) -> list[EngineComp]:
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
        return comps

    @staticmethod
    def _parse(raw: Any) -> Optional[EngineComp]:
        try:
            adjusted = raw.get("adjusted_price_per_carat")
            return EngineComp(
                comp_id=str(raw["comp_id"]),
                source_type=str(raw.get("source_type") or ""),
                source=str(raw.get("source") or "unknown"),
                observed_on=date.fromisoformat(str(raw["observed_on"])[:10]),
                currency=str(raw.get("currency") or "USD"),
                price_total=float(raw["price_total"]),
                carat=float(raw["carat"]),
                similarity=float(raw["similarity"]),
                attributes=dict(raw.get("attributes") or {}),
                adjusted_price_per_carat=float(adjusted) if adjusted is not None else None,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning("dropping malformed comp from engine: %r", raw)
            return None

    def close(self) -> None:
        self._client.close()
