"""
Scenario pricing for unknown treatment or origin.

When treatment is unknown we also price the stone under each plausible
treatment for its variety ("if unheated" vs "if heated"); when origin is
unknown for an origin-sensitive variety we price it under its main origins.
Each scenario obeys the same 5-comp floor as the primary valuation.
"""

from dataclasses import dataclass

from enterprise.engine.base import StoneQuery

TREATMENT_SCENARIOS_BY_VARIETY = {
    "emerald": ("none", "oiled"),
    "tanzanite": ("heated", "none"),
}
DEFAULT_TREATMENT_SCENARIOS = ("none", "heated")

# Varieties whose price depends materially on origin, with the origins to price.
ORIGIN_SCENARIOS = {
    "blue_sapphire": ("kashmir", "burma", "sri_lanka", "madagascar"),
    "ruby": ("burma", "mozambique", "thailand"),
    "emerald": ("colombia", "zambia", "brazil"),
    "spinel": ("burma", "tanzania", "sri_lanka"),
    "padparadscha_sapphire": ("sri_lanka", "madagascar"),
    "paraiba_tourmaline": ("brazil", "mozambique", "nigeria"),
    "alexandrite": ("russia", "brazil", "sri_lanka"),
}

MAX_SCENARIOS = 8

_HEAT_SENSITIVE_SPECIES = {"corundum"}


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    label: str
    assumptions: dict
    query: StoneQuery


def is_origin_sensitive(variety: str) -> bool:
    return variety in ORIGIN_SCENARIOS


def _treatment_label(treatment: str, species: str | None) -> str:
    if treatment == "none":
        return "If unheated" if species in _HEAT_SENSITIVE_SPECIES else "If untreated"
    return f"If {treatment}"


def _origin_label(origin: str) -> str:
    return f"If {origin.replace('_', ' ').title()} origin"


def build_scenarios(query: StoneQuery) -> list[Scenario]:
    scenarios: list[Scenario] = []
    if query.treatment is None:
        options = TREATMENT_SCENARIOS_BY_VARIETY.get(query.variety, DEFAULT_TREATMENT_SCENARIOS)
        for t in options:
            scenarios.append(
                Scenario(
                    scenario_id=f"treatment:{t}",
                    label=_treatment_label(t, query.species),
                    assumptions={"treatment": t},
                    query=query.with_overrides(treatment=t),
                )
            )
    if query.origin is None and is_origin_sensitive(query.variety):
        for o in ORIGIN_SCENARIOS[query.variety]:
            scenarios.append(
                Scenario(
                    scenario_id=f"origin:{o}",
                    label=_origin_label(o),
                    assumptions={"origin": o},
                    query=query.with_overrides(origin=o),
                )
            )
    return scenarios[:MAX_SCENARIOS]
