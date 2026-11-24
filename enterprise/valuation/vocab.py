"""
Vocabulary normalization for stone attributes.

Free-text fields (variety, origin, shape, color grade, clarity) are
normalized to snake_case tokens with a small alias table. Unknown values pass
through unchanged so the engine (which knows 133+ varieties) can decide.
"""

import unicodedata
from typing import Optional

VARIETY_ALIASES = {
    "sapphire": "blue_sapphire",
    "padparadscha": "padparadscha_sapphire",
    "paraiba": "paraiba_tourmaline",
    "red_spinel": "spinel",
}

# Species for the varieties the enterprise layer has rules for (scenarios,
# origin sensitivity). The engine remains the authority for everything else.
VARIETY_SPECIES = {
    "blue_sapphire": "corundum",
    "pink_sapphire": "corundum",
    "yellow_sapphire": "corundum",
    "padparadscha_sapphire": "corundum",
    "ruby": "corundum",
    "emerald": "beryl",
    "aquamarine": "beryl",
    "morganite": "beryl",
    "spinel": "spinel",
    "tanzanite": "zoisite",
    "paraiba_tourmaline": "tourmaline",
    "tourmaline": "tourmaline",
    "alexandrite": "chrysoberyl",
}

ORIGIN_ALIASES = {
    "myanmar": "burma",
    "burmese": "burma",
    "ceylon": "sri_lanka",
    "srilanka": "sri_lanka",
    "kashmiri": "kashmir",
    "colombian": "colombia",
    "zambian": "zambia",
    "mozambican": "mozambique",
    "madagascan": "madagascar",
}

TREATMENT_ALIASES = {
    "none": "none",
    "no": "none",
    "untreated": "none",
    "unheated": "none",
    "no_heat": "none",
    "nh": "none",
    "no_oil": "none",
    "no_indications_of_heating": "none",
    "heated": "heated",
    "heat": "heated",
    "h": "heated",
    "heat_treated": "heated",
    "oiled": "oiled",
    "oil": "oiled",
    "minor_oil": "oiled",
    "moderate_oil": "oiled",
    "clarity_enhanced": "oiled",
    "diffused": "diffused",
    "diffusion": "diffused",
    "beryllium_diffused": "diffused",
    "lattice_diffused": "diffused",
    "filled": "filled",
    "glass_filled": "filled",
    "lead_glass_filled": "filled",
    "fracture_filled": "filled",
    "irradiated": "irradiated",
    "other": "other",
}

TREATMENTS = ("none", "heated", "oiled", "diffused", "filled", "irradiated", "other")
UNKNOWN_TOKENS = {"", "unknown", "n_a", "na", "not_known", "unspecified"}

LABS = {
    "gia": "GIA",
    "agl": "AGL",
    "ssef": "SSEF",
    "gubelin": "Gubelin",
    "grs": "GRS",
}


def token(value: Optional[str]) -> Optional[str]:
    """Lowercase, strip accents, collapse whitespace/hyphens to underscores."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().lower()
    for ch in (" ", "-", "/", "."):
        text = text.replace(ch, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def normalize_variety(value: str) -> str:
    t = token(value) or ""
    return VARIETY_ALIASES.get(t, t)


def species_for(variety: str) -> Optional[str]:
    return VARIETY_SPECIES.get(variety)


def normalize_origin(value: Optional[str]) -> Optional[str]:
    t = token(value)
    if t is None or t in UNKNOWN_TOKENS:
        return None
    return ORIGIN_ALIASES.get(t, t)


def normalize_treatment(value: Optional[str]) -> Optional[str]:
    """Map a treatment description to the controlled vocabulary; None = unknown.

    Raises ValueError for values that cannot be mapped.
    """
    t = token(value)
    if t is None or t in UNKNOWN_TOKENS:
        return None
    if t in TREATMENT_ALIASES:
        return TREATMENT_ALIASES[t]
    raise ValueError(
        f"unrecognized treatment {value!r}; use one of {list(TREATMENTS)} or omit for unknown"
    )


def normalize_lab(value: Optional[str]) -> Optional[str]:
    t = token(value)
    if t is None or t == "":
        return None
    if t in LABS:
        return LABS[t]
    raise ValueError(f"unsupported lab {value!r}; supported: {sorted(LABS.values())}")


def normalize_free(value: Optional[str]) -> Optional[str]:
    t = token(value)
    return t or None
