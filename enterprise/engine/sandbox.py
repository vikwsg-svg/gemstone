"""
Sandbox scan engine: example scan results for ``gsk_test_`` keys.

It does not look at the images and its prices are not market values. The
same photos always return the same example (or the example for the item type
the caller states), so integrators can build against the real response shape.
Every example covers an outcome the live API can return:

- ring, pendant, bracelet: identified and priced, with comparables;
- loose ruby: identified, but only 3 comparables, so the price is withheld;
- photos under 2 KB each: not identified, no price.

Every response built on it carries ``synthetic_data: true``.
"""

import hashlib
from collections.abc import Mapping
from datetime import date
from typing import Any, Optional

from enterprise.engine.base import (
    AnalysisResult,
    Comparable,
    PendingAnalysis,
    PhotoRef,
    PriceContext,
    ScanEngine,
    UnsupportedModelVersion,
)

MODEL_VERSION = "sandbox-2026.11"
MIN_USABLE_BYTES = 2000


def _c(source: str, title: str, price: float, price_type: str, observed: str,
       *differences: str) -> Comparable:
    return Comparable(source=source, title=title, price=price, currency="USD",
                      price_type=price_type, observed_on=date.fromisoformat(observed),
                      differences=differences)


_EXAMPLES: dict[str, tuple[AnalysisResult, list[Comparable]]] = {
    "ring": (
        AnalysisResult(
            analysis_id="sbx_ring",
            model_version=MODEL_VERSION,
            identified=True,
            summary="Platinum ring with an oval blue sapphire of about 2.1 ct and diamond side stones",
            confidence=0.86,
            observations=(
                "Oval center stone, saturated medium-dark blue, about 9 x 7 mm",
                "Fourteen small round side stones set along the shoulders",
                "White metal; a PT950 stamp is visible inside the band",
            ),
            alternatives=(
                {"label": "Center stone is a blue spinel", "probability": 0.08},
                {"label": "Center stone is a lab-grown sapphire", "probability": 0.06},
            ),
            open_questions=("Is the sapphire heated?", "Where is the sapphire from?"),
            next_evidence=(
                "A lab report for the center stone (GIA, SSEF, Gubelin, AGL or GRS) would settle "
                "treatment and origin",
            ),
            price_context=PriceContext(
                status="provided", low=2400, high=6600, basis="retail replacement",
                explanation="Most comparable rings with heated sapphires of this size list "
                            "between $2,400 and $6,600; unheated stones sit at the top of that "
                            "range and above.",
            ),
        ),
        [
            _c("sandbox-dealer-1", "Platinum sapphire ring, 2.10 ct, diamond shoulders", 9448,
               "asking", "2024-06-26", "Heated sapphire, Sri Lanka", "Fewer side stones"),
            _c("sandbox-auction-1", "Sapphire and diamond ring, platinum", 5200, "realized",
               "2025-07-11", "Slightly larger center stone (2.3 ct)"),
            _c("sandbox-dealer-2", "14k gold sapphire ring, 2.27 ct", 3082, "asking",
               "2023-12-08", "14k gold instead of platinum"),
            _c("sandbox-marketplace-1", "Oval sapphire halo ring, platinum", 4450, "asking",
               "2026-03-02", "Halo setting"),
            _c("sandbox-auction-2", "Platinum ring with blue sapphire, 1.9 ct", 2663, "realized",
               "2025-11-19", "Smaller center stone", "Diffusion-treated sapphire"),
            _c("sandbox-dealer-3", "Sapphire and diamond three-stone ring, platinum", 6100,
               "asking", "2026-05-14", "Two larger side diamonds"),
            _c("sandbox-resale-1", "Platinum sapphire ring, 2.0 ct", 3900, "realized",
               "2026-01-28", "Plain band"),
            _c("sandbox-dealer-4", "Blue sapphire ring, platinum, 2.2 ct", 5650, "asking",
               "2026-08-30", "Unheated per seller (no report)"),
        ],
    ),
    "pendant": (
        AnalysisResult(
            analysis_id="sbx_pendant",
            model_version=MODEL_VERSION,
            identified=True,
            summary="18k yellow gold pendant with a round diamond of about 1 ct",
            confidence=0.78,
            observations=("Round brilliant center stone, about 6.4 mm across",
                          "Four-prong setting; a 750 stamp is visible on the bail"),
            alternatives=({"label": "Center stone is a lab-grown diamond", "probability": 0.30},
                          {"label": "Center stone is moissanite", "probability": 0.04}),
            open_questions=("Is the diamond natural or lab-grown?",),
            next_evidence=("A grading report, or a photo of the girdle inscription, would show "
                           "whether the diamond is natural or lab-grown",),
            price_context=PriceContext(
                status="provided", low=1800, high=5200, basis="retail replacement",
                explanation="The low end assumes a lab-grown diamond and the high end a natural "
                            "one; a grading report would narrow this a lot.",
            ),
        ),
        [
            _c("sandbox-dealer-1", "18k gold solitaire pendant, 1.01 ct natural", 5400, "asking",
               "2026-02-11", "Natural diamond, G VS2"),
            _c("sandbox-resale-1", "Diamond solitaire pendant, 18k, 0.95 ct", 3100, "realized",
               "2025-12-03"),
            _c("sandbox-marketplace-1", "Lab-grown diamond pendant, 1.0 ct, 18k", 1650, "asking",
               "2026-06-21", "Lab-grown diamond"),
            _c("sandbox-auction-1", "Gold and diamond pendant, 1.1 ct", 3800, "realized",
               "2025-09-17", "Bezel setting"),
            _c("sandbox-dealer-2", "Lab-grown solitaire pendant, 18k yellow, 1.05 ct", 1900,
               "asking", "2026-07-04", "Lab-grown diamond"),
            _c("sandbox-dealer-3", "18k diamond pendant, 1.0 ct, H SI1", 4300, "asking",
               "2026-04-09", "Lower clarity"),
            _c("sandbox-resale-2", "Yellow gold diamond pendant, 0.9 ct", 2700, "realized",
               "2026-03-15", "Smaller diamond"),
        ],
    ),
    "bracelet": (
        AnalysisResult(
            analysis_id="sbx_bracelet",
            model_version=MODEL_VERSION,
            identified=True,
            summary="18k yellow gold curb-link bracelet without stones",
            confidence=0.90,
            observations=("Curb-link bracelet, yellow metal", "A 750 stamp is visible on the clasp"),
            alternatives=({"label": "14k gold with a misleading stamp", "probability": 0.05},),
            open_questions=("How much does it weigh?", "Is there a maker's mark?"),
            next_evidence=("Weigh the bracelet in grams; most of its value is in the gold",
                           "A close photo of the clasp would show any maker's mark"),
            price_context=PriceContext(
                status="provided", low=1500, high=2900, basis="resale",
                explanation="Unsigned 18k link bracelets resell mostly on gold weight; the range "
                            "assumes 20-30 g.",
            ),
        ),
        [
            _c("sandbox-resale-1", "18k gold curb bracelet, 24 g", 2350, "realized", "2026-05-02"),
            _c("sandbox-dealer-1", "18k yellow gold curb link bracelet", 3200, "asking",
               "2026-06-18", "Heavier (31 g)"),
            _c("sandbox-auction-1", "Gold link bracelet, 18k", 1900, "realized", "2025-10-24",
               "Lighter (19 g)"),
            _c("sandbox-marketplace-1", "18k curb chain bracelet, 7.5 in", 2600, "asking",
               "2026-08-07"),
            _c("sandbox-resale-2", "Yellow gold curb bracelet, 18k, 26 g", 2500, "realized",
               "2026-02-20"),
            _c("sandbox-dealer-2", "18k gold bracelet, curb links, 22 g", 2750, "asking",
               "2026-09-12"),
        ],
    ),
    "loose_stone": (
        AnalysisResult(
            analysis_id="sbx_loose_ruby",
            model_version=MODEL_VERSION,
            identified=True,
            summary="Loose cushion-cut ruby of about 1.5 ct",
            confidence=0.70,
            observations=("Cushion cut, medium red with a slight purple tint", "About 7 x 6 mm"),
            alternatives=({"label": "Red spinel", "probability": 0.15},
                          {"label": "Garnet", "probability": 0.07}),
            open_questions=("Is it a ruby or a red spinel?", "Is it heated?", "Where is it from?"),
            next_evidence=("A lab report would confirm the species and settle treatment and origin",),
            price_context=PriceContext(
                status="provided", low=900, high=4800, basis="retail replacement",
                explanation="Rubies of this size range widely with treatment and origin.",
            ),
        ),
        [
            _c("sandbox-dealer-1", "Cushion ruby, 1.52 ct, heated", 2100, "asking", "2026-04-30"),
            _c("sandbox-auction-1", "Unheated ruby, 1.4 ct, Burma", 6800, "realized", "2025-06-12",
               "Unheated, Burmese origin (report)"),
            _c("sandbox-marketplace-1", "Ruby, cushion, 1.6 ct", 1250, "asking", "2026-07-19",
               "Glass-filled per seller"),
        ],
    ),
}
_ORDER = ["ring", "pendant", "bracelet", "loose_stone"]

_UNIDENTIFIED = AnalysisResult(
    analysis_id="sbx_unidentified",
    model_version=MODEL_VERSION,
    identified=False,
    summary="The photos are too small or unclear to identify the item",
    confidence=0.0,
    next_evidence=("1 to 5 clear, well-lit photos of the same item: front, side, and any stamps "
                   "inside the band or on the clasp",),
    price_context=PriceContext(status="withheld",
                               withheld_reason="The item could not be identified."),
)


class SandboxScanEngine(ScanEngine):
    name = "sandbox"
    synthetic = True

    def analyze(self, photos: list[PhotoRef], details: Mapping[str, Any],
                model_version: Optional[str]) -> AnalysisResult:
        if model_version not in (None, MODEL_VERSION):
            raise UnsupportedModelVersion(model_version, [MODEL_VERSION])
        if photos and all(p.bytes is not None and p.bytes < MIN_USABLE_BYTES for p in photos):
            return _UNIDENTIFIED
        stated = details.get("item_type")
        if stated in _EXAMPLES:
            return _EXAMPLES[stated][0]
        digest = hashlib.sha256("".join(sorted(p.sha256 for p in photos)).encode()).digest()
        return _EXAMPLES[_ORDER[digest[0] % len(_ORDER)]][0]

    def get(self, analysis_id: str) -> "AnalysisResult | PendingAnalysis":
        for result, _ in _EXAMPLES.values():
            if result.analysis_id == analysis_id:
                return result
        return _UNIDENTIFIED

    def comparables(self, analysis_id: str) -> list[Comparable]:
        for result, comps in _EXAMPLES.values():
            if result.analysis_id == analysis_id:
                return list(comps)
        return []
