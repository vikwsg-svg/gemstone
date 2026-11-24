"""
Confidence score (0-1) with machine-readable reason codes.

The score starts at 1.0 and each applicable reason subtracts its penalty.
With fewer than 5 qualifying comps there is no price, the score is 0 and the
level is "none". Penalties are deliberately simple and transparent; the
pricing team should calibrate them against held-out realized sales.
"""

from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Optional

from enterprise.engine.base import ItemQuery, StoneSpec
from enterprise.valuation.scenarios import is_origin_sensitive

MIN_COMPS = 5  # hard floor, enforced in service.py; duplicated here for messages only
FEW_COMPS_BELOW = 10
RARE_ORIGIN_BELOW = 3
STALE_DAYS = 730
WIDE_BAND_RATIO = 0.6
LOW_SIMILARITY_BELOW = 0.70
LAB_REPORT_EXPECTED_FROM_CARAT = 1.0  # for stones set in pieces

UNCERTAIN_IDENTIFICATION_BELOW = 0.75

PENALTIES = {
    "uncertain_identification": 0.15,
    "few_comps": 0.15,
    "missing_growth": 0.45,  # natural vs lab-grown is often a 10x difference
    "missing_treatment": 0.15,
    "missing_origin": 0.10,
    "rare_origin": 0.15,
    "missing_metal": 0.15,
    "missing_metal_weight": 0.10,
    "unsigned_brand": 0.10,
    "missing_condition": 0.05,
    "asking_only": 0.10,
    "wide_band": 0.10,
    "stale_comps": 0.10,
    "low_similarity": 0.10,
    "no_lab_report": 0.05,
}

REASON_CODES = ("unidentified", "insufficient_comps", *PENALTIES)

# Where treatment is a primary price driver (unheated corundum or no-oil emerald
# can be worth multiples of the treated equivalent), not knowing it costs more.
TREATMENT_SENSITIVE_SPECIES = {"corundum"}
TREATMENT_SENSITIVE_VARIETIES = {"emerald"}
MISSING_TREATMENT_SENSITIVE_PENALTY = 0.30


def _treatment_sensitive(stone: StoneSpec) -> bool:
    return (
        stone.species in TREATMENT_SENSITIVE_SPECIES
        or stone.variety in TREATMENT_SENSITIVE_VARIETIES
    )


@dataclass(frozen=True)
class UsedComp:
    """The facts confidence needs about each comp that was used."""

    similarity: float
    source_type: str
    observed_on: date
    origin: Optional[str]


def _reason(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _n(count: int, noun: str = "comparable") -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _stone_reasons(query: ItemQuery, stone: StoneSpec, used: list[UsedComp]) -> list[dict]:
    reasons = []
    what = "The stone" if query.is_loose else "The center stone"
    if stone.variety == "diamond":
        if stone.growth is None:
            reasons.append(_reason(
                "missing_growth",
                f"{what} is a diamond but natural vs lab-grown was not provided; the two are "
                "priced as separate markets. See scenarios for each case.",
            ))
    elif stone.treatment is None:
        reasons.append(_reason(
            "missing_treatment",
            "Treatment was not provided, so comparables include treated and untreated "
            "stones. See scenarios for each case.",
        ))
    if stone.origin is None and is_origin_sensitive(stone.variety):
        reasons.append(_reason(
            "missing_origin",
            "Origin was not provided for a variety where origin moves price. "
            "See scenarios for the main origins.",
        ))
    if stone.origin is not None:
        same_origin = sum(1 for c in used if c.origin == stone.origin)
        if same_origin < RARE_ORIGIN_BELOW:
            reasons.append(_reason(
                "rare_origin",
                f"Few comparables share the stated origin ({stone.origin}): "
                f"{same_origin} of {len(used)}. The rest are from other origins.",
            ))
    lab_expected = query.is_loose or stone.carat / max(stone.count, 1) >= LAB_REPORT_EXPECTED_FROM_CARAT
    if stone.lab is None and lab_expected:
        reasons.append(_reason(
            "no_lab_report", "No lab report was provided; stone attributes are unverified."
        ))
    return reasons


def _piece_reasons(query: ItemQuery) -> list[dict]:
    reasons = []
    if query.metal is None:
        reasons.append(_reason(
            "missing_metal", "Metal was not provided; comparables span metals and purities."
        ))
    elif query.metal.weight_grams is None:
        reasons.append(_reason(
            "missing_metal_weight", "Metal weight was not provided; material value is estimated."
        ))
    if query.brand is not None and query.signed is not True:
        reasons.append(_reason(
            "unsigned_brand",
            "A maker is named but a signature or maker's mark was not confirmed; "
            "any brand premium is uncertain.",
        ))
    if query.condition is None:
        reasons.append(_reason("missing_condition", "Condition was not provided."))
    return reasons


def assess(
    *,
    query: ItemQuery,
    used: list[UsedComp],
    qualified_count: int,
    band_mid_low_high: Optional[dict],
    today: date,
    identification_confidence: Optional[float] = None,
) -> dict:
    """``band_mid_low_high`` is the per-carat band (loose stones) or total band (pieces).

    ``identification_confidence`` is set when the item was identified from photos.
    """
    reasons: list[dict] = []
    priced = band_mid_low_high is not None

    if identification_confidence is not None and identification_confidence < UNCERTAIN_IDENTIFICATION_BELOW:
        reasons.append(_reason(
            "uncertain_identification",
            f"The identification from the photos is preliminary ({identification_confidence:.0%} "
            "confident). See the alternatives and the next evidence that would settle it.",
        ))

    if not priced:
        reasons.append(_reason(
            "insufficient_comps",
            f"Only {_n(qualified_count, 'qualifying comparable')}; at least {MIN_COMPS} "
            "are required, so no price is returned.",
        ))
    elif qualified_count < FEW_COMPS_BELOW:
        reasons.append(_reason(
            "few_comps",
            f"Priced on {_n(qualified_count, 'qualifying comparable')}; "
            f"{FEW_COMPS_BELOW} or more give a tighter estimate.",
        ))

    stone = query.principal
    if stone is not None:
        reasons.extend(_stone_reasons(query, stone, used))
    if not query.is_loose:
        reasons.extend(_piece_reasons(query))

    # Statistics over the comps only mean something when there are enough to price.
    if priced and used and all(c.source_type == "asking" for c in used):
        reasons.append(_reason(
            "asking_only", "All comparables are asking prices; none are realized sales."
        ))
    if priced and band_mid_low_high["mid"] > 0:
        spread = (band_mid_low_high["high"] - band_mid_low_high["low"]) / band_mid_low_high["mid"]
        if spread > WIDE_BAND_RATIO:
            reasons.append(_reason("wide_band", "Comparable prices are widely dispersed for this item."))
    if priced and used:
        if median((today - c.observed_on).days for c in used) > STALE_DAYS:
            reasons.append(_reason("stale_comps", "The median comparable is more than 24 months old."))
        if sum(c.similarity for c in used) / len(used) < LOW_SIMILARITY_BELOW:
            reasons.append(_reason("low_similarity", "Comparables are only loosely similar to this item."))

    if not priced:
        return {"score": 0.0, "level": "none", "reasons": reasons}

    penalties = dict(PENALTIES)
    if stone is not None and _treatment_sensitive(stone):
        penalties["missing_treatment"] = MISSING_TREATMENT_SENSITIVE_PENALTY
    score = 1.0 - sum(penalties.get(r["code"], 0.0) for r in reasons)
    score = round(max(0.05, min(1.0, score)), 2)
    level = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
    return {"score": score, "level": level, "reasons": reasons}
