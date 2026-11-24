"""
Sandbox identifier: SIMULATED identification for the developer sandbox.

It does not look at the images. The same photos always return the same
synthetic identification, chosen from a few sample items (or matched to the
item type the caller states), so integrators can exercise the whole Scan API
flow with a ``gsk_test_`` key. Photos that are tiny (under 2 KB each) are
treated as unusable and return ``unidentified``, so that path can be tested.
Every response built on it carries ``synthetic_data: true``.
"""

import hashlib
from collections.abc import Mapping
from typing import Any

from enterprise.engine.base import Identification, IdentifyEngine, PhotoRef

MIN_USABLE_BYTES = 2000

_SAMPLES: dict[str, Identification] = {
    "ring": Identification(
        status="identified",
        item={
            "item_type": "ring",
            "metal": {"type": "platinum", "purity": "950"},
            "stones": [
                {"role": "center", "variety": "blue sapphire", "carat": 2.1, "shape": "oval"},
                {"role": "side", "variety": "diamond", "carat": 0.6, "count": 14,
                 "shape": "round", "growth": "natural"},
            ],
            "condition": "excellent",
        },
        confidence=0.86,
        summary="Platinum ring with an oval blue sapphire of about 2.1 ct and diamond side stones",
        observations=(
            "Oval center stone, saturated medium-dark blue, about 9 x 7 mm",
            "Fourteen small round side stones set along the shoulders",
            "White metal; a PT950 stamp is visible inside the band",
        ),
        alternatives=(
            {"label": "Center stone is a blue spinel", "probability": 0.08},
            {"label": "Center stone is a lab-grown sapphire", "probability": 0.06},
        ),
        open_questions=("Is the sapphire heated?", "Where is the sapphire from?",
                        "How much does the ring weigh?"),
        next_evidence=(
            "A lab report for the center stone (GIA, SSEF, Gubelin, AGL or GRS) would settle "
            "treatment and origin",
            "Weigh the ring in grams to firm up the metal value",
        ),
    ),
    "pendant": Identification(
        status="identified",
        item={
            "item_type": "pendant",
            "metal": {"type": "gold", "purity": "18k", "color": "yellow"},
            "stones": [{"role": "center", "variety": "diamond", "carat": 1.0, "shape": "round"}],
            "condition": "very_good",
        },
        confidence=0.78,
        summary="18k yellow gold pendant with a round diamond of about 1 ct",
        observations=(
            "Round brilliant center stone, about 6.4 mm across",
            "Yellow metal four-prong setting; a 750 stamp is visible on the bail",
        ),
        alternatives=(
            {"label": "Center stone is a lab-grown diamond", "probability": 0.30},
            {"label": "Center stone is moissanite", "probability": 0.04},
        ),
        open_questions=("Is the diamond natural or lab-grown?", "What are its color and clarity?"),
        next_evidence=(
            "A grading report, or a photo of the girdle inscription, would show whether the "
            "diamond is natural or lab-grown",
        ),
    ),
    "bracelet": Identification(
        status="identified",
        item={
            "item_type": "bracelet",
            "metal": {"type": "gold", "purity": "18k", "color": "yellow"},
            "era": "contemporary",
            "condition": "good",
        },
        confidence=0.90,
        summary="18k yellow gold link bracelet without stones",
        observations=("Curb-link bracelet, yellow metal", "A 750 stamp is visible on the clasp"),
        alternatives=({"label": "14k gold with a misleading stamp", "probability": 0.05},),
        open_questions=("How much does it weigh?", "Is there a maker's mark?"),
        next_evidence=(
            "Weigh the bracelet in grams; most of its value is in the gold",
            "A close photo of the clasp would show any maker's mark",
        ),
    ),
    "loose_stone": Identification(
        status="identified",
        item={
            "item_type": "loose_stone",
            "stones": [{"role": "center", "variety": "ruby", "carat": 1.5, "shape": "cushion"}],
        },
        confidence=0.70,
        summary="Loose cushion-cut ruby of about 1.5 ct",
        observations=("Cushion cut, medium red with a slight purple tint", "About 7 x 6 mm"),
        alternatives=(
            {"label": "Red spinel", "probability": 0.15},
            {"label": "Garnet", "probability": 0.07},
        ),
        open_questions=("Is it a ruby or a red spinel?", "Is it heated?", "Where is it from?"),
        next_evidence=(
            "A lab report would confirm the species and settle treatment and origin",
        ),
    ),
}
_ORDER = ["ring", "pendant", "bracelet", "loose_stone"]

_UNIDENTIFIED = Identification(
    status="unidentified",
    confidence=0.0,
    summary="The photos are too small or unclear to identify the item",
    next_evidence=(
        "1 to 5 clear, well-lit photos of the same item: front, side, and any stamps inside "
        "the band or on the clasp",
    ),
)


class SandboxIdentifier(IdentifyEngine):
    name = "sandbox"
    synthetic = True

    def identify(self, photos: list[PhotoRef], hints: Mapping[str, Any]) -> Identification:
        if photos and all(p.bytes is not None and p.bytes < MIN_USABLE_BYTES for p in photos):
            return _UNIDENTIFIED
        stated_type = hints.get("item_type")
        if stated_type in _SAMPLES:
            return _SAMPLES[stated_type]
        if stated_type is not None:
            return Identification(
                status="identified",
                item={"item_type": stated_type},
                confidence=0.6,
                summary=f"Item identified as {stated_type.replace('_', ' ')} from the stated details",
                open_questions=("What metal and stones does it have?",),
                next_evidence=("State the metal, stones and maker, or add clearer photos",),
            )
        digest = hashlib.sha256("".join(sorted(p.sha256 for p in photos)).encode()).digest()
        return _SAMPLES[_ORDER[digest[0] % len(_ORDER)]]
