"""
Scenario pricing for unknowns on the principal stone of a loose stone or a piece.

- Diamond with unknown growth: priced as natural and as lab-grown.
- Colored stone with unknown treatment: priced under each plausible treatment
  ("if unheated" vs "if heated").
- Colored stone with unknown origin, for an origin-sensitive variety: priced
  under its main origins.

Each scenario obeys the same 5-comp floor as the primary valuation.
"""

from dataclasses import dataclass

from enterprise.engine.base import ItemQuery

TREATMENT_SCENARIOS_BY_VARIETY = {
    "emerald": ("none", "oiled"),
    "tanzanite": ("heated", "none"),
}
DEFAULT_TREATMENT_SCENARIOS = ("none", "heated")
GROWTH_SCENARIOS = ("natural", "lab_grown")

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
    query: ItemQuery


def is_origin_sensitive(variety: str) -> bool:
    return variety in ORIGIN_SCENARIOS


def _treatment_label(treatment: str, species: str | None) -> str:
    if treatment == "none":
        return "If unheated" if species in _HEAT_SENSITIVE_SPECIES else "If untreated"
    return f"If {treatment}"


def build_scenarios(query: ItemQuery) -> list[Scenario]:
    stone = query.principal
    if stone is None:
        return []
    scenarios: list[Scenario] = []
    if stone.variety == "diamond":
        if stone.growth is None:
            for g in GROWTH_SCENARIOS:
                scenarios.append(Scenario(
                    scenario_id=f"growth:{g}",
                    label="If natural" if g == "natural" else "If lab-grown",
                    assumptions={"growth": g},
                    query=query.with_principal(growth=g),
                ))
        return scenarios

    if stone.treatment is None:
        for t in TREATMENT_SCENARIOS_BY_VARIETY.get(stone.variety, DEFAULT_TREATMENT_SCENARIOS):
            scenarios.append(Scenario(
                scenario_id=f"treatment:{t}",
                label=_treatment_label(t, stone.species),
                assumptions={"treatment": t},
                query=query.with_principal(treatment=t),
            ))
    if stone.origin is None and is_origin_sensitive(stone.variety):
        for o in ORIGIN_SCENARIOS[stone.variety]:
            scenarios.append(Scenario(
                scenario_id=f"origin:{o}",
                label=f"If {o.replace('_', ' ').title()} origin",
                assumptions={"origin": o},
                query=query.with_principal(origin=o),
            ))
    return scenarios[:MAX_SCENARIOS]
