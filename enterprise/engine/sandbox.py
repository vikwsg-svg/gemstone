"""
Sandbox engine: deterministic SYNTHETIC jewelry and stones for the developer sandbox.

This is not the V2 pricing engine and its numbers are not market values. It
exists so that (a) prospects and integrators can exercise the full Comps API
contract with a ``gsk_test_`` key, as the platform spec requires ("sandbox
with synthetic stones"), and (b) the enterprise rules can be tested end to
end without the production engine. Every comp it returns is marked
``attributes.synthetic = true`` and every valuation built on it carries
``synthetic_data: true``.

The dataset covers loose colored stones, loose diamonds (natural and
lab-grown) and finished pieces (rings, earrings, necklaces, pendants,
bracelets, brooches) with metal, center and side stones, maker, era and
condition. Makers are fictional. It is generated from a fixed seed, so
results are reproducible. Some cells are deliberately thin (alexandrite,
Kashmir sapphire) to exercise the 5-comp floor and the rare-origin code.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Optional

from enterprise.engine.base import (
    CompsEngine,
    EngineComp,
    EngineResult,
    ItemQuery,
    StoneSpec,
)

ANCHOR_DATE = date(2026, 10, 31)
SEED = 20261101

# ── Synthetic stone price models ─────────────────────────────────────────────


@dataclass(frozen=True)
class _ColoredSpec:
    species: str
    base_ppc: float  # synthetic USD/ct at 1 ct, untreated, medium color, eye clean, realized
    size_exponent: float
    count: int
    origins: dict[str, tuple[float, float]]  # origin -> (price multiplier, frequency weight)
    treatments: dict[str, tuple[float, float]]  # treatment -> (price multiplier, frequency)


_COLORED: dict[str, _ColoredSpec] = {
    "blue_sapphire": _ColoredSpec(
        "corundum", 1400, 0.45, 360,
        {"sri_lanka": (1.0, 30), "madagascar": (0.85, 30), "burma": (1.6, 12),
         "thailand": (0.6, 12), "australia": (0.5, 10), "kashmir": (4.0, 1)},
        {"none": (1.0, 35), "heated": (0.55, 60), "diffused": (0.12, 5)},
    ),
    "ruby": _ColoredSpec(
        "corundum", 2600, 0.55, 300,
        {"burma": (2.0, 15), "mozambique": (1.0, 45), "thailand": (0.7, 20), "madagascar": (0.6, 20)},
        {"none": (1.0, 30), "heated": (0.5, 60), "filled": (0.08, 10)},
    ),
    "pink_sapphire": _ColoredSpec(
        "corundum", 900, 0.4, 200,
        {"sri_lanka": (1.0, 40), "madagascar": (0.85, 50), "burma": (1.3, 10)},
        {"none": (1.0, 35), "heated": (0.6, 65)},
    ),
    "padparadscha_sapphire": _ColoredSpec(
        "corundum", 3500, 0.5, 60,
        {"sri_lanka": (1.0, 60), "madagascar": (0.75, 40)},
        {"none": (1.0, 45), "heated": (0.6, 55)},
    ),
    "emerald": _ColoredSpec(
        "beryl", 2200, 0.5, 300,
        {"colombia": (1.6, 40), "zambia": (1.0, 40), "brazil": (0.6, 20)},
        {"none": (1.0, 10), "oiled": (0.6, 80), "filled": (0.25, 10)},
    ),
    "spinel": _ColoredSpec(
        "spinel", 1300, 0.45, 220,
        {"burma": (1.4, 40), "tanzania": (1.0, 25), "sri_lanka": (0.9, 20), "vietnam": (0.8, 15)},
        {"none": (1.0, 90), "heated": (0.6, 10)},
    ),
    "tanzanite": _ColoredSpec(
        "zoisite", 420, 0.3, 240, {"tanzania": (1.0, 100)}, {"heated": (1.0, 92), "none": (1.15, 8)},
    ),
    "aquamarine": _ColoredSpec(
        "beryl", 220, 0.35, 200,
        {"brazil": (1.1, 40), "mozambique": (1.0, 30), "nigeria": (0.9, 30)},
        {"heated": (1.0, 70), "none": (1.1, 30)},
    ),
    "paraiba_tourmaline": _ColoredSpec(
        "tourmaline", 5500, 0.5, 40,
        {"brazil": (1.8, 25), "mozambique": (1.0, 55), "nigeria": (0.9, 20)},
        {"none": (1.0, 40), "heated": (0.85, 60)},
    ),
    "alexandrite": _ColoredSpec(
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

# Diamonds (natural vs lab-grown are separate markets).
_DIAMOND = {  # growth -> (base USD/ct at 1 ct G VS2 excellent, size exponent, loose count)
    "natural": (3800.0, 0.9, 400),
    "lab_grown": (260.0, 0.45, 300),
}
_D_COLOR_ORDER = ["k", "j", "i", "h", "g", "f", "e", "d"]
_D_COLORS = {"d": (1.45, 6), "e": (1.32, 10), "f": (1.2, 16), "g": (1.0, 22), "h": (0.88, 20),
             "i": (0.76, 12), "j": (0.66, 9), "k": (0.58, 5)}
_D_CLARITY_ORDER = ["si2", "si1", "vs2", "vs1", "vvs2", "vvs1", "if"]
_D_CLARITIES = {"if": (1.5, 4), "vvs1": (1.35, 8), "vvs2": (1.25, 12), "vs1": (1.12, 22),
                "vs2": (1.0, 24), "si1": (0.85, 20), "si2": (0.72, 10)}
_D_CUTS = {"excellent": (1.0, 60), "very_good": (0.9, 30), "good": (0.78, 10)}
_D_SHAPES = {"round": 45, "oval": 12, "cushion": 10, "emerald": 8, "princess": 8, "pear": 7,
             "radiant": 5, "marquise": 3, "heart": 2}
_D_LABS = {"natural": {"GIA": 60, "IGI": 10, "HRD": 5, None: 25},
           "lab_grown": {"IGI": 70, "GIA": 15, None: 15}}
_SIDE_DIAMOND_PPC = {"natural": 900.0, "lab_grown": 120.0}
_SIDE_COLORED_PPC = 300.0

# ── Synthetic jewelry model ──────────────────────────────────────────────────

_ITEM_TYPES = {  # type -> (frequency, metal weight range g, make rate, center-stone probability)
    "ring": (35, (3.0, 8.0), 0.25, 0.80),
    "earrings": (15, (3.0, 10.0), 0.30, 0.70),
    "necklace": (15, (8.0, 40.0), 0.35, 0.35),
    "pendant": (15, (2.0, 8.0), 0.25, 0.80),
    "bracelet": (15, (10.0, 40.0), 0.35, 0.30),
    "brooch": (5, (8.0, 25.0), 0.40, 0.50),
}
_PIECE_COUNT = 1100
_METALS = [  # (type, purity, colors, frequency)
    ("gold", "18k", {"yellow": 50, "white": 35, "rose": 15}, 35),
    ("gold", "14k", {"yellow": 50, "white": 35, "rose": 15}, 35),
    ("platinum", "950", {"white": 1}, 25),
    ("silver", "925", {"white": 1}, 5),
]
_PURITY_FRACTION = {"24k": 0.999, "22k": 0.916, "18k": 0.75, "14k": 0.585, "10k": 0.417,
                    "950": 0.95, "900": 0.90, "925": 0.925}
_DEFAULT_PURITY = {"gold": 0.70, "platinum": 0.95, "silver": 0.925, "palladium": 0.95, "other": 0.5}
_SPOT_PER_GRAM = {"gold": 75.0, "platinum": 32.0, "silver": 0.95, "palladium": 35.0, "other": 0.0}
_CENTERS = {  # (variety, growth) -> frequency, for pieces that have a center stone
    ("diamond", "natural"): 50, ("diamond", "lab_grown"): 16, ("blue_sapphire", None): 14,
    ("ruby", None): 8, ("emerald", None): 12,
}
_BRANDS = {None: (1.0, 85), "maison_aurelle": (2.6, 6), "atelier_verin": (2.0, 5),
           "casa_lumen": (1.6, 4)}
_ERAS = {"contemporary": (1.0, 70), "art_deco": (1.5, 8), "retro": (1.15, 6),
         "victorian": (1.2, 6), "edwardian": (1.4, 4), "mid_century": (1.1, 6)}
_COND_ORDER = ["poor", "fair", "good", "very_good", "excellent", "new"]
_CONDITIONS = {"new": (1.05, 5), "excellent": (1.0, 40), "very_good": (0.92, 28),
               "good": (0.8, 18), "fair": (0.65, 9)}

_REALIZED_SOURCES = ["auction:sandbox-house-1", "auction:sandbox-house-2", "resale:sandbox-platform-1"]
_ASKING_SOURCES = ["dealer:sandbox-dealer-1", "dealer:sandbox-dealer-2",
                   "dealer:sandbox-dealer-3", "marketplace:sandbox-market-1"]
_ASKING_PREMIUM = 1.3  # synthetic: list prices sit above transaction prices

# Similarity weights per engine version, so that pinned versions visibly differ.
_W_COLORED = {
    "v2.4": {"carat": 0.38, "origin": 0.16, "shape": 0.10, "color": 0.20, "clarity": 0.10, "lab": 0.06},
    "v2.3": {"carat": 0.50, "origin": 0.10, "shape": 0.15, "color": 0.15, "clarity": 0.10, "lab": 0.0},
}
_W_DIAMOND = {
    "v2.4": {"carat": 0.40, "color": 0.20, "clarity": 0.15, "cut": 0.05, "shape": 0.12, "lab": 0.08},
    "v2.3": {"carat": 0.50, "color": 0.15, "clarity": 0.15, "cut": 0.05, "shape": 0.15, "lab": 0.0},
}
_W_PIECE = {
    "v2.4": {"center_carat": 0.20, "center_quality": 0.12, "metal": 0.10, "purity": 0.05,
             "weight": 0.12, "side": 0.06, "brand": 0.18, "era": 0.10, "condition": 0.07},
    "v2.3": {"center_carat": 0.25, "center_quality": 0.10, "metal": 0.12, "purity": 0.05,
             "weight": 0.15, "side": 0.08, "brand": 0.12, "era": 0.08, "condition": 0.05},
}


def _pick(rng: random.Random, table: dict) -> Any:
    keys = list(table)
    weights = [v[-1] if isinstance(v, tuple) else v for v in table.values()]
    return rng.choices(keys, weights=weights, k=1)[0]


def _source(rng: random.Random) -> tuple[str, str, float]:
    realized = rng.random() < 0.3
    source = rng.choice(_REALIZED_SOURCES if realized else _ASKING_SOURCES)
    return ("realized" if realized else "asking"), source, (1.0 if realized else _ASKING_PREMIUM)


def _date(rng: random.Random) -> date:
    return ANCHOR_DATE - timedelta(days=rng.randint(0, 1095))


def _sample_stone(rng: random.Random, variety: str, growth: Optional[str], carat: float) -> dict:
    if variety == "diamond":
        return {
            "variety": "diamond", "species": "diamond", "carat": carat, "growth": growth,
            "shape": _pick(rng, _D_SHAPES), "color_grade": _pick(rng, _D_COLORS),
            "clarity": _pick(rng, _D_CLARITIES), "cut_grade": _pick(rng, _D_CUTS),
            "treatment": None, "origin": None, "lab": _pick(rng, _D_LABS[growth]),
        }
    spec = _COLORED[variety]
    return {
        "variety": variety, "species": spec.species, "carat": carat, "growth": "natural",
        "origin": _pick(rng, spec.origins), "treatment": _pick(rng, spec.treatments),
        "color_grade": _pick(rng, _COLORS), "clarity": _pick(rng, _CLARITIES),
        "shape": _pick(rng, _SHAPES), "cut_grade": None, "lab": _pick(rng, _LABS),
    }


def _stone_ppc(s: dict) -> Optional[float]:
    """Synthetic USD/ct for a stone description (unknown attributes count as neutral)."""
    variety, carat = s.get("variety"), s.get("carat") or 0.0
    if carat <= 0:
        return None
    if variety == "diamond":
        growth = s.get("growth")
        if growth is None:  # unknown: midpoint of the two markets on a log scale
            return math.sqrt(_stone_ppc({**s, "growth": "natural"}) * _stone_ppc({**s, "growth": "lab_grown"}))
        base, exp, _ = _DIAMOND[growth]
        return (base * carat ** exp * _D_COLORS.get(s.get("color_grade"), (1.0,))[0]
                * _D_CLARITIES.get(s.get("clarity"), (1.0,))[0] * _D_CUTS.get(s.get("cut_grade"), (1.0,))[0])
    spec = _COLORED.get(variety)
    if spec is None:
        return None
    origin = spec.origins.get(s.get("origin"), (1.0,))[0]
    treatment = spec.treatments.get(s.get("treatment"))
    t_mult = treatment[0] if treatment else sum(m * w for m, w in spec.treatments.values()) / sum(
        w for _, w in spec.treatments.values())
    return (spec.base_ppc * carat ** spec.size_exponent * origin * t_mult
            * _COLORS.get(s.get("color_grade"), (1.0,))[0] * _CLARITIES.get(s.get("clarity"), (1.0,))[0])


@lru_cache(maxsize=1)
def _dataset() -> dict[str, list[dict]]:
    """Synthetic records indexed by 'loose:<variety>' and 'piece:<item_type>'."""
    rng = random.Random(SEED)
    out: dict[str, list[dict]] = defaultdict(list)

    # Loose colored stones (generated first, in a fixed order, so data is stable).
    for variety, spec in _COLORED.items():
        for i in range(spec.count):
            carat = round(min(15.0, max(0.3, math.exp(rng.gauss(math.log(1.8), 0.6)))), 2)
            origin = _pick(rng, spec.origins)
            treatment = _pick(rng, spec.treatments)
            color = _pick(rng, _COLORS)
            clarity = _pick(rng, _CLARITIES)
            shape = _pick(rng, _SHAPES)
            lab = _pick(rng, _LABS)
            source_type, source, premium = _source(rng)
            stone = {"variety": variety, "species": spec.species, "carat": carat, "shape": shape,
                     "color_grade": color, "clarity": clarity, "treatment": treatment,
                     "origin": origin, "lab": lab, "growth": "natural", "cut_grade": None}
            ppc = _stone_ppc(stone) * premium * math.exp(rng.gauss(0, 0.15))
            out[f"loose:{variety}"].append({
                "comp_id": f"sbx_{variety}_{i:04d}", "item_type": "loose_stone", "stone": stone,
                "source_type": source_type, "source": source, "observed_on": _date(rng),
                "price_total": float(round(ppc * carat)),
            })

    # Loose diamonds.
    for growth, (_, _, count) in _DIAMOND.items():
        for i in range(count):
            carat = round(min(10.0, max(0.3, math.exp(rng.gauss(math.log(1.1), 0.5)))), 2)
            stone = _sample_stone(rng, "diamond", growth, carat)
            source_type, source, premium = _source(rng)
            ppc = _stone_ppc(stone) * premium * math.exp(rng.gauss(0, 0.12))
            out["loose:diamond"].append({
                "comp_id": f"sbx_diamond_{growth}_{i:04d}", "item_type": "loose_stone",
                "stone": stone, "source_type": source_type, "source": source,
                "observed_on": _date(rng), "price_total": float(round(ppc * carat)),
            })

    # Finished pieces.
    for i in range(_PIECE_COUNT):
        item_type = _pick(rng, {k: v[0] for k, v in _ITEM_TYPES.items()})
        _, (w_lo, w_hi), make_rate, p_center = _ITEM_TYPES[item_type]
        metal_type, purity, colors, _ = rng.choices(_METALS, weights=[m[3] for m in _METALS])[0]
        metal = {"type": metal_type, "purity": purity, "color": _pick(rng, colors),
                 "weight_grams": round(rng.uniform(w_lo, w_hi), 1)}
        center = None
        if rng.random() < p_center:
            variety, growth = _pick(rng, _CENTERS)
            mu = 0.9 if variety == "diamond" else 1.6
            carat = round(min(8.0, max(0.25, math.exp(rng.gauss(math.log(mu), 0.5)))), 2)
            center = _sample_stone(rng, variety, growth, carat)
        side = None
        if rng.random() < 0.5:
            side = {"variety": "diamond", "growth": "lab_grown" if rng.random() < 0.15 else "natural",
                    "carat": round(rng.uniform(0.1, 1.5), 2), "count": rng.randint(6, 40)}
        brand = _pick(rng, _BRANDS)
        era = _pick(rng, _ERAS)
        condition = _pick(rng, _CONDITIONS)
        source_type, source, premium = _source(rng)

        metal_value = metal["weight_grams"] * _PURITY_FRACTION[purity] * _SPOT_PER_GRAM[metal_type]
        stones_value = (_stone_ppc(center) * center["carat"]) if center else 0.0
        if side:
            stones_value += _SIDE_DIAMOND_PPC[side["growth"]] * side["carat"]
        price = ((metal_value + stones_value) * (1 + make_rate) * _BRANDS[brand][0]
                 * _ERAS[era][0] * _CONDITIONS[condition][0] * premium * math.exp(rng.gauss(0, 0.18)))
        out[f"piece:{item_type}"].append({
            "comp_id": f"sbx_{item_type}_{i:04d}", "item_type": item_type, "metal": metal,
            "center": center, "side": side, "brand": brand, "signed": brand is not None,
            "era": era, "condition": condition, "source_type": source_type, "source": source,
            "observed_on": _date(rng), "price_total": float(round(max(price, 20.0))),
        })
    return dict(out)


# ── Similarity ───────────────────────────────────────────────────────────────


def _ordered(query: Optional[str], comp: Optional[str], order: list[str]) -> float:
    if query is None:
        return 0.7
    if query == comp:
        return 1.0
    if query in order and comp in order:
        gap = abs(order.index(query) - order.index(comp))
        return 0.55 if gap == 1 else 0.2
    return 0.5


def _closeness(q: Optional[float], c: Optional[float], scale: float, unknown: float = 0.7) -> float:
    if not q or not c:
        return unknown
    return math.exp(-abs(math.log(q / c)) / scale)


def _stone_quality(q: StoneSpec, c: dict) -> float:
    if q.variety == "diamond":
        return (_ordered(q.color_grade, c.get("color_grade"), _D_COLOR_ORDER)
                + _ordered(q.clarity, c.get("clarity"), _D_CLARITY_ORDER)) / 2
    return (_ordered(q.color_grade, c.get("color_grade"), _COLOR_ORDER)
            + _ordered(q.clarity, c.get("clarity"), _CLARITY_ORDER)) / 2


def _stone_matches(q: StoneSpec, c: Optional[dict]) -> bool:
    """Hard filters: same variety; known treatment and known growth are separate markets."""
    if c is None or c["variety"] != q.variety:
        return False
    if q.variety == "diamond":
        return q.growth is None or c.get("growth") == q.growth
    return q.treatment is None or c.get("treatment") == q.treatment


def _raw_value(rec: dict, center: Optional[dict]) -> float:
    """Materials value of a synthetic piece (metal + stones) under the synthetic model."""
    metal = rec["metal"]
    value = metal["weight_grams"] * _PURITY_FRACTION[metal["purity"]] * _SPOT_PER_GRAM[metal["type"]]
    if center:
        value += _stone_ppc(center) * center["carat"]
    if rec["side"]:
        value += _SIDE_DIAMOND_PPC[rec["side"]["growth"]] * rec["side"]["carat"]
    return value


def _center(query: ItemQuery) -> Optional[StoneSpec]:
    return next((s for s in query.stones if s.role == "center"), None)


def _side_carat(query: ItemQuery) -> float:
    return sum(s.carat for s in query.stones if s.role != "center")


def _brand_score(q: Optional[str], c: Optional[str]) -> float:
    # A named maker is hard-filtered in _pieces; this scores unbranded queries.
    if q is None:
        return 1.0 if c is None else 0.5
    return 1.0 if q == c else 0.0


class SandboxEngine(CompsEngine):
    name = "sandbox"
    synthetic = True

    def supports(self, engine_version: str) -> bool:
        return engine_version in _W_PIECE

    @staticmethod
    def varieties() -> list[str]:
        return sorted([*_COLORED, "diamond"])

    def find_comps(self, query: ItemQuery, *, engine_version: str, limit: int) -> EngineResult:
        if query.is_loose:
            comps = self._loose(query, engine_version)
            shares = None
        else:
            comps = self._pieces(query, engine_version)
            shares = self._breakdown(query)
        comps.sort(key=lambda c: (-c.similarity, c.comp_id))
        return EngineResult(comps=comps[:limit], breakdown_shares=shares)

    # loose stones ------------------------------------------------------------

    def _loose(self, query: ItemQuery, version: str) -> list[EngineComp]:
        q = query.principal
        out = []
        spec = _COLORED.get(q.variety)
        for rec in _dataset().get(f"loose:{q.variety}", ()):
            s = rec["stone"]
            if not _stone_matches(q, s):
                continue
            if q.variety == "diamond":
                w = _W_DIAMOND[version]
                parts = {
                    "carat": _closeness(q.carat, s["carat"], 0.25),
                    "color": _ordered(q.color_grade, s["color_grade"], _D_COLOR_ORDER),
                    "clarity": _ordered(q.clarity, s["clarity"], _D_CLARITY_ORDER),
                    "cut": 0.7 if q.cut_grade is None else (1.0 if q.cut_grade == s["cut_grade"] else 0.5),
                    "shape": 1.0 if q.shape == s["shape"] else 0.55,
                    "lab": 0.8 if q.lab is None else (1.0 if s["lab"] else 0.5),
                }
            else:
                w = _W_COLORED[version]
                parts = {
                    "carat": _closeness(q.carat, s["carat"], 0.35),
                    "origin": 0.7 if q.origin is None else (1.0 if q.origin == s["origin"] else 0.4),
                    "shape": 1.0 if q.shape == s["shape"] else 0.55,
                    "color": _ordered(q.color_grade, s["color_grade"], _COLOR_ORDER),
                    "clarity": _ordered(q.clarity, s["clarity"], _CLARITY_ORDER),
                    "lab": 0.8 if q.lab is None else (1.0 if s["lab"] else 0.5),
                }
            similarity = sum(w[k] * parts[k] for k in w) / sum(w.values())
            adjusted = None
            if (version == "v2.4" and spec is not None and q.origin in spec.origins
                    and q.origin != s["origin"]):
                factor = spec.origins[q.origin][0] / spec.origins[s["origin"]][0]
                adjusted = round(rec["price_total"] / s["carat"] * factor, 2)
            out.append(EngineComp(
                comp_id=rec["comp_id"], source_type=rec["source_type"], source=rec["source"],
                observed_on=rec["observed_on"], currency="USD", price_total=rec["price_total"],
                carat=s["carat"], similarity=round(similarity, 4),
                attributes={"item_type": "loose_stone", **{k: v for k, v in s.items() if k != "carat"},
                            "synthetic": True},
                adjusted_price_per_carat=adjusted,
            ))
        return out

    # finished pieces ---------------------------------------------------------

    def _pieces(self, query: ItemQuery, version: str) -> list[EngineComp]:
        w = _W_PIECE[version]
        center = _center(query)
        q_side = _side_carat(query)
        metal = query.metal
        out = []
        for rec in _dataset().get(f"piece:{query.item_type}", ()):
            c_center = rec["center"]
            if center is None and c_center is not None:
                continue
            if center is not None and not _stone_matches(center, c_center):
                continue
            # A named maker is a separate market: compare only with that maker's pieces.
            if query.brand is not None and rec["brand"] != query.brand:
                continue
            c_side = rec["side"]["carat"] if rec["side"] else 0.0
            origin_score = 1.0
            if center is not None and center.variety != "diamond" and center.origin is not None:
                origin_score = 1.0 if center.origin == c_center.get("origin") else 0.4
            parts = {
                "center_carat": _closeness(center.carat, c_center["carat"], 0.35) if center else 1.0,
                "center_quality": (_stone_quality(center, c_center) + origin_score) / 2
                if center else 1.0,
                "metal": 0.7 if metal is None else (1.0 if metal.type == rec["metal"]["type"] else 0.35),
                "purity": 0.75 if metal is None or metal.purity is None
                else (1.0 if metal.purity == rec["metal"]["purity"] else 0.7),
                "weight": _closeness(metal.weight_grams if metal else None,
                                     rec["metal"]["weight_grams"], 0.5),
                "side": 1.0 if q_side == 0 and c_side == 0
                else (0.5 if (q_side == 0) != (c_side == 0) else _closeness(q_side, c_side, 0.6)),
                "brand": _brand_score(query.brand, rec["brand"]),
                "era": 0.7 if query.era is None else (1.0 if query.era == rec["era"] else 0.4),
                "condition": _ordered(query.condition, rec["condition"], _COND_ORDER),
            }
            similarity = sum(w[k] * parts[k] for k in w) / sum(w.values())
            # v2.4 adjusts each comp toward the subject: the center stone's origin
            # (re-valuing that stone within the piece) and the piece's condition.
            factor = 1.0
            if version == "v2.4":
                spec = _COLORED.get(c_center["variety"]) if c_center else None
                if (center is not None and spec is not None and center.origin in spec.origins
                        and center.origin != c_center.get("origin")):
                    raw = _raw_value(rec, c_center)
                    swapped = {**c_center, "origin": center.origin}
                    delta = (_stone_ppc(swapped) - _stone_ppc(c_center)) * c_center["carat"]
                    factor *= (raw + delta) / raw
                if query.condition in _CONDITIONS and query.condition != rec["condition"]:
                    factor *= _CONDITIONS[query.condition][0] / _CONDITIONS[rec["condition"]][0]
            adjusted = round(rec["price_total"] * factor, 2) if factor != 1.0 else None
            attrs = {
                "item_type": rec["item_type"],
                "metal_type": rec["metal"]["type"], "metal_purity": rec["metal"]["purity"],
                "metal_color": rec["metal"]["color"], "metal_weight_grams": rec["metal"]["weight_grams"],
                "center_variety": c_center["variety"] if c_center else None,
                "center_carat": c_center["carat"] if c_center else None,
                "center_growth": c_center["growth"] if c_center else None,
                "center_color_grade": c_center["color_grade"] if c_center else None,
                "center_clarity": c_center["clarity"] if c_center else None,
                "treatment": c_center["treatment"] if c_center else None,
                "origin": c_center["origin"] if c_center else None,
                "side_carat_total": c_side or None,
                "side_count": rec["side"]["count"] if rec["side"] else None,
                "brand": rec["brand"], "signed": rec["signed"], "era": rec["era"],
                "condition": rec["condition"], "synthetic": True,
            }
            out.append(EngineComp(
                comp_id=rec["comp_id"], source_type=rec["source_type"], source=rec["source"],
                observed_on=rec["observed_on"], currency="USD", price_total=rec["price_total"],
                carat=None, similarity=round(similarity, 4), attributes=attrs,
                adjusted_price_total=adjusted,
            ))
        return out

    @staticmethod
    def _breakdown(query: ItemQuery) -> Optional[dict[str, float]]:
        """Raw/Make/Market shares of the subject piece under the synthetic model."""
        _, (w_lo, w_hi), make_rate, _ = _ITEM_TYPES.get(query.item_type, (0, (5.0, 15.0), 0.3, 0))
        metal_value = 0.0
        if query.metal is not None:
            weight = query.metal.weight_grams or (w_lo + w_hi) / 2
            purity = _PURITY_FRACTION.get(query.metal.purity or "", _DEFAULT_PURITY[query.metal.type])
            metal_value = weight * purity * _SPOT_PER_GRAM[query.metal.type]
        stones_value = 0.0
        for s in query.stones:
            if s.role == "center" or s.count == 1:
                ppc = _stone_ppc({"variety": s.variety, "carat": s.carat / s.count,
                                  "growth": s.growth, "origin": s.origin, "treatment": s.treatment,
                                  "color_grade": s.color_grade, "clarity": s.clarity,
                                  "cut_grade": s.cut_grade})
            elif s.variety == "diamond":
                ppc = _SIDE_DIAMOND_PPC.get(s.growth or "natural")
            else:
                ppc = _SIDE_COLORED_PPC
            stones_value += (ppc or _SIDE_COLORED_PPC) * s.carat
        raw = metal_value + stones_value
        make = raw * make_rate
        premium = _BRANDS.get(query.brand, (1.0,))[0] * _ERAS.get(query.era, (1.0,))[0]
        market = max(0.0, (raw + make) * (premium - 1.0))
        total = raw + make + market
        if total <= 0:
            return None
        return {"raw": raw / total, "make": make / total, "market": market / total}
