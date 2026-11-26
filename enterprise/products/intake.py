"""
Intake: route each consigned item before a specialist touches it.

The scan identifies and prices the item; Intake applies the account's routing
rules and logs the decision with its reasons and the rules it used:

1. Not identified, or no price            -> review
2. High end below ``decline_below_value``  -> decline
3. Low end at or above ``lab_min_value``   -> lab
4. A lab report is suggested and the value range is wide
   (high >= ``lab_spread_ratio`` x low), so the open question moves the value -> lab
5. Identified with confidence >= ``min_confidence_to_list`` and high end
   at or below ``list_max_value``          -> list
6. Otherwise                               -> review

Prefilled listing fields are suggestions from the scan. Treatment, origin and
maker attribution are never filled in as fact.

Pilot metric: of the items routed to ``list`` that a specialist later
reviewed, the share whose final decision was something else (target under 2%).
"""

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise.models import ProductRecord
from enterprise.products.common import EvalContext, ProductHandler, rate

DECISIONS = ("list", "review", "lab", "decline")
_LAB_WORDS = re.compile(r"\blab\b|\blaboratory\b|grading report", re.IGNORECASE)


class IntakeRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    list_max_value: float = Field(default=2000, ge=0,
                                  description="List directly only when the high end is at or below this.")
    min_confidence_to_list: float = Field(default=0.85, ge=0, le=1,
                                          description="Minimum identification confidence to list directly.")
    lab_min_value: float = Field(default=5000, ge=0,
                                 description="Send to lab when the low end is at or above this.")
    decline_below_value: float = Field(default=50, ge=0,
                                       description="Decline when the high end is below this.")
    lab_spread_ratio: float = Field(default=2.0, ge=1,
                                    description="Send to lab when a lab report is suggested and "
                                                "high >= this x low.")

    @model_validator(mode="after")
    def _ordered(self) -> "IntakeRules":
        if self.decline_below_value >= self.lab_min_value:
            raise ValueError("decline_below_value must be below lab_min_value")
        return self


def _money(value: float, currency: str) -> str:
    return f"${value:,.0f}" if currency == "USD" else f"{value:,.0f} {currency}"


def _reason(code: str, message: str) -> dict:
    return {"code": code, "message": message}


class IntakeHandler(ProductHandler):
    product = "intake"
    object_name = "intake_item"
    id_prefix = "int"
    path = "intake/items"
    result_key = "routing"
    settings_model = IntakeRules
    review_outcomes = DECISIONS
    summary_columns = ("decision", "reason_codes")

    def summarize(self, record: ProductRecord, block: dict) -> dict:
        return {"decision": block.get("decision"),
                "reason_codes": [r["code"] for r in block.get("reasons", [])]}

    def agrees(self, record: ProductRecord, review_outcome: str) -> bool:
        return review_outcome == record.outcome

    def events(self, record: ProductRecord) -> list[str]:
        return ["intake.item.completed"]

    def stats(self, rows: list[ProductRecord]) -> dict:
        by_decision = {d: sum(r.outcome == d for r in rows) for d in DECISIONS}
        reviewed = [r for r in rows if r.review_outcome]
        listed = [r for r in reviewed if r.outcome == "list"]
        corrected = [r for r in listed if r.review_outcome != "list"]
        return {
            "items": len(rows),
            "by_decision": by_decision,
            "reviewed": len(reviewed),
            "agreement_rate": rate(sum(r.review_outcome == r.outcome for r in reviewed), len(reviewed)),
            "list_routed_reviewed": len(listed),
            "list_routed_corrected": len(corrected),
            "list_correction_rate": rate(len(corrected), len(listed)),
        }

    def evaluate(self, ctx: EvalContext) -> tuple[dict, Optional[str], Optional[int]]:
        rules = IntakeRules.model_validate(ctx.settings)
        ident = ctx.scan["identification"]
        price = ctx.scan["price_context"]
        confidence = ident.get("confidence") or 0.0
        reasons: list[dict] = []

        if ident["status"] != "identified":
            decision = "review"
            reasons.append(_reason("not_identified",
                                   "The scan could not identify the item, so a specialist should look."))
        elif price["status"] != "provided":
            decision = "review"
            reasons.append(_reason("no_price", ("The scan gave no price. "
                                                + (price.get("withheld_reason") or "")).strip()))
        else:
            low, high, cur = price["low"], price["high"], price["currency"]
            lab_suggested = any(_LAB_WORDS.search(e) for e in ident.get("next_evidence", []))
            if high < rules.decline_below_value:
                decision = "decline"
                reasons.append(_reason("below_floor", f"The high end ({_money(high, cur)}) is below your "
                                       f"floor of {_money(rules.decline_below_value, cur)}."))
            elif low >= rules.lab_min_value:
                decision = "lab"
                reasons.append(_reason("above_lab_threshold", f"The low end ({_money(low, cur)}) is at or "
                                       f"above your lab threshold of {_money(rules.lab_min_value, cur)}."))
            elif lab_suggested and high >= rules.lab_spread_ratio * low:
                decision = "lab"
                reasons.append(_reason("lab_settles_value", "A lab report is suggested, and the open "
                                       f"questions move the value between {_money(low, cur)} and "
                                       f"{_money(high, cur)}."))
            elif confidence >= rules.min_confidence_to_list and high <= rules.list_max_value:
                decision = "list"
                reasons.append(_reason("clear_to_list", f"Identified with {confidence:.0%} confidence and "
                                       f"valued at up to {_money(high, cur)}, within your limit of "
                                       f"{_money(rules.list_max_value, cur)}."))
            else:
                decision = "review"
                if confidence < rules.min_confidence_to_list:
                    reasons.append(_reason("confidence_below_list", f"Identification confidence "
                                           f"({confidence:.0%}) is below your listing minimum of "
                                           f"{rules.min_confidence_to_list:.0%}."))
                if high > rules.list_max_value:
                    reasons.append(_reason("value_above_list", f"The high end ({_money(high, cur)}) is "
                                           f"above your direct-listing limit of "
                                           f"{_money(rules.list_max_value, cur)}."))

        prefill = {
            "note": "Suggestions from the scan. Treatment, origin and maker attribution are never "
                    "filled in as fact.",
            "title": {"value": ident.get("summary"), "source": "scan",
                      "confidence": ident.get("confidence")} if ident.get("summary") else None,
            "price_range": ({"low": price["low"], "high": price["high"], "currency": price["currency"],
                             "basis": price.get("basis"), "source": "scan"}
                            if price["status"] == "provided" else None),
            "open_questions": ident.get("open_questions", []),
            "consignor_stated": ctx.product_input.get("details") or {},
        }
        block = {
            "decision": decision,
            "reasons": reasons,
            "rules_applied": rules.model_dump(mode="json"),
            "consignor_description": ctx.product_input.get("consignor_description"),
            "prefill": prefill,
        }
        return block, decision, None
