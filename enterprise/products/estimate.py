"""
Estimate: a documented starting point for pre-sale estimates and reserves.

It supports the specialist's judgment rather than replacing it. The estimate
is built only from realized sales among the scan's comparables:

- at least 5 realized sales -> low/high estimate = the middle half of their
  prices (25th to 75th percentile), low rounded down and high rounded up to
  auction increments;
- fewer -> no estimate: the comparables and the scan's price context are
  shown for reference ("comps and context only").

The reserve is the house's own policy (a percentage of the low estimate),
rounded down to an increment; none is suggested until the policy is set.
Asking prices are listed separately and never used for the number.
"""

import math
from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.products.common import EvalContext, ProductHandler

MIN_REALIZED = 5


class Increment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    up_to: Optional[float] = Field(default=None, gt=0, description="Applies below this value; null = above all.")
    step: float = Field(gt=0)


DEFAULT_INCREMENTS = [
    {"up_to": 1000, "step": 100},
    {"up_to": 5000, "step": 500},
    {"up_to": 20000, "step": 1000},
    {"up_to": 50000, "step": 5000},
    {"up_to": 200000, "step": 10000},
    {"up_to": None, "step": 50000},
]


class EstimatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reserve_pct_of_low: Optional[float] = Field(
        default=None, gt=0, le=1,
        description="Your house's reserve as a share of the low estimate, e.g. 0.8. "
                    "No reserve is suggested until this is set.",
    )
    increments: list[Increment] = Field(
        default_factory=lambda: [Increment(**i) for i in DEFAULT_INCREMENTS],
        description="Rounding steps for estimates and reserves, in ascending order.",
    )

    @model_validator(mode="after")
    def _ascending(self) -> "EstimatePolicy":
        bounds = [i.up_to for i in self.increments]
        if not bounds or bounds[-1] is not None or any(b is None for b in bounds[:-1]):
            raise ValueError("increments must end with one entry whose up_to is null")
        finite = bounds[:-1]
        if finite != sorted(finite) or len(set(finite)) != len(finite):
            raise ValueError("increments must be in ascending order of up_to")
        return self


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile of sorted ``values``."""
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def step_for(value: float, increments: list[Increment]) -> float:
    for inc in increments:
        if inc.up_to is None or value < inc.up_to:
            return inc.step
    return increments[-1].step  # pragma: no cover


def round_down(value: float, increments: list[Increment]) -> int:
    step = step_for(value, increments)
    return int(max(step, math.floor(value / step) * step))


def round_up(value: float, increments: list[Increment]) -> int:
    step = step_for(value, increments)
    return int(math.ceil(value / step) * step)


def _money(value: float, currency: str) -> str:
    return f"${value:,.0f}" if currency == "USD" else f"{value:,.0f} {currency}"


class EstimateHandler(ProductHandler):
    product = "estimate"
    object_name = "estimate"
    id_prefix = "est"
    result_key = "estimate"
    settings_model = EstimatePolicy

    def evaluate(self, ctx: EvalContext) -> tuple[dict, Optional[str], Optional[int]]:
        policy = EstimatePolicy.model_validate(ctx.settings)
        ident = ctx.scan["identification"]
        currency = ctx.scan["price_context"].get("currency") or "USD"
        items = ctx.scan["comparables"]["items"]
        realized = sorted((c for c in items if c["price_type"] == "realized" and c["currency"] == currency),
                          key=lambda c: c["price"])
        asking = [c for c in items if c["price_type"] == "asking"]
        other_currency = [c for c in items if c["price_type"] == "realized" and c["currency"] != currency]

        block = {
            "status": "no_estimate",
            "currency": currency,
            "low": None,
            "high": None,
            "reserve": None,
            "reserve_policy_pct": policy.reserve_pct_of_low,
            "based_on": None,
            "rationale": None,
            "reason": None,
            "lot_number": ctx.product_input.get("lot_number"),
            "sale": ctx.product_input.get("sale"),
            "realized_comparables": realized,
            "asking_comparables": asking,
        }
        if other_currency:
            block["notes"] = [f"{len(other_currency)} realized sales in other currencies were not used."]

        if ident["status"] != "identified":
            block["reason"] = "The item could not be identified, so no estimate is given."
            return block, "no_estimate", None
        if len(realized) < MIN_REALIZED:
            block["reason"] = (f"Only {len(realized)} realized sale{'s' if len(realized) != 1 else ''} "
                               f"among the comparables; at least {MIN_REALIZED} are needed for an "
                               "estimate. The comparables and the scan's price context are shown for "
                               "reference.")
            return block, "no_estimate", None

        prices = [c["price"] for c in realized]
        low = round_down(quantile(prices, 0.25), policy.increments)
        high = round_up(quantile(prices, 0.75), policy.increments)
        if high <= low:
            high = low + int(step_for(low, policy.increments))
        reserve = (round_down(low * policy.reserve_pct_of_low, policy.increments)
                   if policy.reserve_pct_of_low else None)
        dates = sorted(date.fromisoformat(c["observed_on"]) for c in realized if c.get("observed_on"))
        median = quantile(prices, 0.5)

        if not dates:
            period = ""
        elif f"{dates[0]:%b %Y}" == f"{dates[-1]:%b %Y}":
            period = f" in {dates[0]:%b %Y}"
        else:
            period = f" between {dates[0]:%b %Y} and {dates[-1]:%b %Y}"
        sentences = [
            f"Based on {len(realized)} realized sales of comparable items{period}, with a median of "
            f"{_money(median, currency)}, we suggest an estimate of {_money(low, currency)}–"
            f"{_money(high, currency)}."
        ]
        if reserve is not None:
            sentences.append(f"At your reserve policy of {policy.reserve_pct_of_low:.0%} of the low "
                             f"estimate, the reserve would be {_money(reserve, currency)}.")
        if asking:
            many = len(asking) != 1
            sentences.append(f"{len(asking)} asking price{'s are' if many else ' is'} listed "
                             f"separately and {'were' if many else 'was'} not used.")
        if ident.get("open_questions"):
            sentences.append("Still open: " + " ".join(ident["open_questions"]))

        block.update(
            status="estimated", low=low, high=high, reserve=reserve,
            based_on={"realized_sales": len(realized),
                      "from": dates[0].isoformat() if dates else None,
                      "to": dates[-1].isoformat() if dates else None,
                      "median": round(median, 2)},
            rationale=" ".join(sentences),
        )
        if reserve is None:
            block["notes"] = block.get("notes", []) + [
                "No reserve suggested: set your house's reserve policy (reserve_pct_of_low)."]
        return block, "estimated", None
