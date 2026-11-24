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

from enterprise.engine.base import StoneQuery
from enterprise.valuation.scenarios import is_origin_sensitive

MIN_COMPS = 5  # hard floor, enforced in service.py; duplicated here for messages only
FEW_COMPS_BELOW = 10
RARE_ORIGIN_BELOW = 3
STALE_DAYS = 730
WIDE_BAND_RATIO = 0.6
LOW_SIMILARITY_BELOW = 0.70

PENALTIES = {
    "few_comps": 0.15,
    "missing_treatment": 0.15,
    "missing_origin": 0.10,
    "rare_origin": 0.15,
    "asking_only": 0.10,
    "wide_band": 0.10,
    "stale_comps": 0.10,
    "low_similarity": 0.10,
    "no_lab_report": 0.05,
}

REASON_CODES = ("insufficient_comps", *PENALTIES)


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


def assess(
    *,
    query: StoneQuery,
    used: list[UsedComp],
    qualified_count: int,
    band_per_carat: Optional[dict],
    today: date,
) -> dict:
    reasons: list[dict] = []

    if band_per_carat is None:
        reasons.append(
            _reason(
                "insufficient_comps",
                f"Only {_n(qualified_count, 'qualifying comparable')}; at least {MIN_COMPS} "
                "are required, so no price is returned.",
            )
        )
    elif qualified_count < FEW_COMPS_BELOW:
        reasons.append(
            _reason(
                "few_comps",
                f"Priced on {qualified_count} qualifying comparables; "
                f"{FEW_COMPS_BELOW} or more give a tighter estimate.",
            )
        )

    if query.treatment is None:
        reasons.append(
            _reason(
                "missing_treatment",
                "Treatment was not provided, so comparables include treated and untreated "
                "stones. See scenarios for each case.",
            )
        )
    if query.origin is None and is_origin_sensitive(query.variety):
        reasons.append(
            _reason(
                "missing_origin",
                "Origin was not provided for a variety where origin moves price. "
                "See scenarios for the main origins.",
            )
        )
    if query.origin is not None:
        same_origin = sum(1 for c in used if c.origin == query.origin)
        if same_origin < RARE_ORIGIN_BELOW:
            reasons.append(
                _reason(
                    "rare_origin",
                    f"Few comparables share the stated origin ({query.origin}): "
                    f"{same_origin} of {len(used)}. The rest are from other origins.",
                )
            )
    # Statistics over the comps only mean something when there are enough to price.
    priced = band_per_carat is not None
    if priced and all(c.source_type == "asking" for c in used):
        reasons.append(
            _reason(
                "asking_only",
                "All comparables are asking prices; none are realized sales.",
            )
        )
    if band_per_carat is not None and band_per_carat["mid"] > 0:
        spread = (band_per_carat["high"] - band_per_carat["low"]) / band_per_carat["mid"]
        if spread > WIDE_BAND_RATIO:
            reasons.append(
                _reason("wide_band", "Comparable prices are widely dispersed for this stone.")
            )
    if priced and used:
        median_age = median((today - c.observed_on).days for c in used)
        if median_age > STALE_DAYS:
            reasons.append(
                _reason("stale_comps", "The median comparable is more than 24 months old.")
            )
        mean_sim = sum(c.similarity for c in used) / len(used)
        if mean_sim < LOW_SIMILARITY_BELOW:
            reasons.append(
                _reason("low_similarity", "Comparables are only loosely similar to this stone.")
            )
    if query.lab is None:
        reasons.append(
            _reason("no_lab_report", "No lab report was provided; attributes are unverified.")
        )

    if band_per_carat is None:
        return {"score": 0.0, "level": "none", "reasons": reasons}

    score = 1.0 - sum(PENALTIES.get(r["code"], 0.0) for r in reasons)
    score = round(max(0.05, min(1.0, score)), 2)
    level = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
    return {"score": score, "level": level, "reasons": reasons}
