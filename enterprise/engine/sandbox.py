"""
Sandbox engine: deterministic SYNTHETIC stones for the developer sandbox.

This is not the V2 pricing engine and its numbers are not market values. It
exists so that (a) prospects and integrators can exercise the full Comps API
contract with a ``gsk_test_`` key, as the platform spec requires ("sandbox
with synthetic stones"), and (b) the enterprise rules can be tested end to
end without the production engine. Every comp it returns is marked
``attributes.synthetic = true`` and every valuation built on it carries
``synthetic_data: true``.

The dataset is generated from a fixed seed, so results are reproducible.
Some cells are deliberately thin (alexandrite, Kashmir sapphire) to exercise
the 5-comp floor and the rare-origin reason code.
"""

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

from enterprise.engine.base import CompsEngine, EngineComp, StoneQuery

ANCHOR_DATE = date(2026, 10, 31)
SEED = 20261101


@dataclass(frozen=True)
class _VarietySpec:
    species: str
    base_ppc: float  # synthetic USD/ct at 1 ct, untreated, medium color, eye clean, realized
    size_exponent: float
    count: int
    origins: dict[str, tuple[float, float]]  # origin -> (price multiplier, frequency weight)
    treatments: dict[str, tuple[float, float]]  # treatment -> (price multiplier, frequency)


_SPECS: dict[str, _VarietySpec] = {
    "blue_sapphire": _VarietySpec(
        "corundum", 1400, 0.45, 360,
        {"sri_lanka": (1.0, 30), "madagascar": (0.85, 30), "burma": (1.6, 12),
         "thailand": (0.6, 12), "australia": (0.5, 10), "kashmir": (4.0, 1)},
        {"none": (1.0, 35), "heated": (0.55, 60), "diffused": (0.12, 5)},
    ),
    "ruby": _VarietySpec(
        "corundum", 2600, 0.55, 300,
        {"burma": (2.0, 15), "mozambique": (1.0, 45), "thailand": (0.7, 20),
         "madagascar": (0.6, 20)},
        {"none": (1.0, 30), "heated": (0.5, 60), "filled": (0.08, 10)},
    ),
    "pink_sapphire": _VarietySpec(
        "corundum", 900, 0.4, 200,
        {"sri_lanka": (1.0, 40), "madagascar": (0.85, 50), "burma": (1.3, 10)},
        {"none": (1.0, 35), "heated": (0.6, 65)},
    ),
    "padparadscha_sapphire": _VarietySpec(
        "corundum", 3500, 0.5, 60,
        {"sri_lanka": (1.0, 60), "madagascar": (0.75, 40)},
        {"none": (1.0, 45), "heated": (0.6, 55)},
    ),
    "emerald": _VarietySpec(
        "beryl", 2200, 0.5, 300,
        {"colombia": (1.6, 40), "zambia": (1.0, 40), "brazil": (0.6, 20)},
        {"none": (1.0, 10), "oiled": (0.6, 80), "filled": (0.25, 10)},
    ),
    "spinel": _VarietySpec(
        "spinel", 1300, 0.45, 220,
        {"burma": (1.4, 40), "tanzania": (1.0, 25), "sri_lanka": (0.9, 20),
         "vietnam": (0.8, 15)},
        {"none": (1.0, 90), "heated": (0.6, 10)},
    ),
    "tanzanite": _VarietySpec(
        "zoisite", 420, 0.3, 240,
        {"tanzania": (1.0, 100)},
        {"heated": (1.0, 92), "none": (1.15, 8)},
    ),
    "aquamarine": _VarietySpec(
        "beryl", 220, 0.35, 200,
        {"brazil": (1.1, 40), "mozambique": (1.0, 30), "nigeria": (0.9, 30)},
        {"heated": (1.0, 70), "none": (1.1, 30)},
    ),
    "paraiba_tourmaline": _VarietySpec(
        "tourmaline", 5500, 0.5, 40,
        {"brazil": (1.8, 25), "mozambique": (1.0, 55), "nigeria": (0.9, 20)},
        {"none": (1.0, 40), "heated": (0.85, 60)},
    ),
    "alexandrite": _VarietySpec(
        "chrysoberyl", 8000, 0.6, 4,
        {"russia": (1.5, 30), "brazil": (1.0, 40), "sri_lanka": (0.8, 30)},
        {"none": (1.0, 100)},
    ),
}

_SHAPES = {"oval": 30, "cushion": 25, "round": 12, "emerald": 10, "pear": 8,
           "octagon": 5, "heart": 4, "marquise": 3, "radiant": 3}
_COLOR_ORDER = ["light", "medium", "intense", "vivid"]
_COLORS = {"vivid": (1.45, 15), "intense": (1.15, 30), "medium": (1.0, 35), "light": (0.6, 20)}
_CLARITY_ORDER = ["included", "slightly_included", "eye_clean"]
_CLARITIES = {"eye_clean": (1.0, 55), "slightly_included": (0.8, 30), "included": (0.5, 15)}
_LABS = {"GIA": 30, "AGL": 10, "SSEF": 10, "Gubelin": 8, "GRS": 12, None: 30}
_REALIZED_SOURCES = ["auction:sandbox-house-1", "auction:sandbox-house-2",
                     "resale:sandbox-platform-1"]
_ASKING_SOURCES = ["dealer:sandbox-dealer-1", "dealer:sandbox-dealer-2",
                   "dealer:sandbox-dealer-3", "marketplace:sandbox-market-1"]
_ASKING_PREMIUM = 1.3  # synthetic: list prices sit above transaction prices

# Similarity weights per engine version. v2.4 adds lab weighting and origin
# adjustment so that pinned versions visibly differ.
_WEIGHTS = {
    "v2.4": {"carat": 0.38, "origin": 0.16, "shape": 0.10, "color": 0.20,
             "clarity": 0.10, "lab": 0.06},
    "v2.3": {"carat": 0.50, "origin": 0.10, "shape": 0.15, "color": 0.15,
             "clarity": 0.10, "lab": 0.0},
}


@dataclass(frozen=True)
class _SyntheticStone:
    comp_id: str
    variety: str
    species: str
    carat: float
    shape: str
    color_grade: str
    clarity: str
    treatment: str
    origin: str
    lab: Optional[str]
    source_type: str
    source: str
    observed_on: date
    price_total: float


def _pick(rng: random.Random, table: dict) -> object:
    keys = list(table)
    weights = [v[1] if isinstance(v, tuple) else v for v in table.values()]
    return rng.choices(keys, weights=weights, k=1)[0]


@lru_cache(maxsize=1)
def _dataset() -> dict[str, tuple[_SyntheticStone, ...]]:
    rng = random.Random(SEED)
    out: dict[str, tuple[_SyntheticStone, ...]] = {}
    for variety, spec in _SPECS.items():
        stones = []
        for i in range(spec.count):
            carat = round(min(15.0, max(0.3, math.exp(rng.gauss(math.log(1.8), 0.6)))), 2)
            origin = _pick(rng, spec.origins)
            treatment = _pick(rng, spec.treatments)
            color = _pick(rng, _COLORS)
            clarity = _pick(rng, _CLARITIES)
            shape = _pick(rng, _SHAPES)
            lab = _pick(rng, _LABS)
            realized = rng.random() < 0.3
            source = rng.choice(_REALIZED_SOURCES if realized else _ASKING_SOURCES)
            ppc = (
                spec.base_ppc
                * carat ** spec.size_exponent
                * spec.origins[origin][0]
                * spec.treatments[treatment][0]
                * _COLORS[color][0]
                * _CLARITIES[clarity][0]
                * (1.0 if realized else _ASKING_PREMIUM)
                * math.exp(rng.gauss(0, 0.15))
            )
            stones.append(
                _SyntheticStone(
                    comp_id=f"sbx_{variety}_{i:04d}",
                    variety=variety,
                    species=spec.species,
                    carat=carat,
                    shape=shape,
                    color_grade=color,
                    clarity=clarity,
                    treatment=treatment,
                    origin=origin,
                    lab=lab,
                    source_type="realized" if realized else "asking",
                    source=source,
                    observed_on=ANCHOR_DATE - timedelta(days=rng.randint(0, 1095)),
                    price_total=float(round(ppc * carat)),
                )
            )
        out[variety] = tuple(stones)
    return out


def _ordered_score(query: Optional[str], comp: str, order: list[str]) -> float:
    if query is None:
        return 0.7
    if query == comp:
        return 1.0
    if query in order:
        gap = abs(order.index(query) - order.index(comp))
        return 0.55 if gap == 1 else 0.2
    return 0.5


class SandboxEngine(CompsEngine):
    name = "sandbox"
    synthetic = True

    def supports(self, engine_version: str) -> bool:
        return engine_version in _WEIGHTS

    @staticmethod
    def varieties() -> list[str]:
        return sorted(_SPECS)

    def find_comps(
        self, query: StoneQuery, *, engine_version: str, limit: int
    ) -> list[EngineComp]:
        weights = _WEIGHTS[engine_version]
        stones = _dataset().get(query.variety, ())
        spec = _SPECS.get(query.variety)
        scored: list[EngineComp] = []
        for s in stones:
            # Known treatment is a hard filter: heated and unheated are different markets.
            if query.treatment is not None and s.treatment != query.treatment:
                continue
            parts = {
                "carat": math.exp(-abs(math.log(query.carat / s.carat)) / 0.35),
                "origin": 0.7 if query.origin is None else (1.0 if query.origin == s.origin else 0.4),
                "shape": 1.0 if query.shape == s.shape else 0.55,
                "color": _ordered_score(query.color_grade, s.color_grade, _COLOR_ORDER),
                "clarity": _ordered_score(query.clarity, s.clarity, _CLARITY_ORDER),
                "lab": 0.8 if query.lab is None else (1.0 if s.lab else 0.5),
            }
            total_w = sum(weights.values())
            similarity = sum(weights[k] * parts[k] for k in weights) / total_w

            adjusted = None
            if (
                engine_version == "v2.4"
                and spec is not None
                and query.origin in spec.origins
                and query.origin != s.origin
            ):
                factor = spec.origins[query.origin][0] / spec.origins[s.origin][0]
                adjusted = round(s.price_total / s.carat * factor, 2)

            scored.append(
                EngineComp(
                    comp_id=s.comp_id,
                    source_type=s.source_type,
                    source=s.source,
                    observed_on=s.observed_on,
                    currency="USD",
                    price_total=s.price_total,
                    carat=s.carat,
                    similarity=round(similarity, 4),
                    attributes={
                        "species": s.species,
                        "variety": s.variety,
                        "shape": s.shape,
                        "color_grade": s.color_grade,
                        "clarity": s.clarity,
                        "treatment": s.treatment,
                        "origin": s.origin,
                        "lab": s.lab,
                        "synthetic": True,
                    },
                    adjusted_price_per_carat=adjusted,
                )
            )
        scored.sort(key=lambda c: (-c.similarity, c.comp_id))
        return scored[:limit]
