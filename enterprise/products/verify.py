"""
Verify: how likely a listing, claim or appraisal is to be wrong, and why.

A risk signal for a person or a rules engine to act on, never an
authentication or a certificate. The scan identifies and prices the item;
Verify runs these checks against what the subject states:

| check                    | fires when                                                   | weight |
| ------------------------ | ------------------------------------------------------------ | ------ |
| price_too_low            | stated value < price_low_ratio x the scan's low end          | 40     |
| price_too_high           | stated value > price_high_ratio x the scan's high end        | 35     |
| unidentified             | the scan could not identify the item                         | 30     |
| identification_uncertain | identification confidence below 60%                          | 20     |
| possible_other_identity  | an alternative identification has probability >= 20%        | 15     |
| duplicate_imagery        | a photo was already used in this account under another reference | 40 |
| seller_pattern           | the seller already has flagged checks in the lookback window | 25     |

Risk score = sum of fired weights, capped at 100. Suggested action: hold at
``hold_at`` and above, manual_review at ``review_at`` and above,
request_lab_report when an identification check fired, otherwise allow.
Duplicate photos are matched by exact fingerprint (sha256) within the
account only; customer data never crosses accounts. Uploaded photos are
fingerprinted by their bytes, linked photos by their URL, so a re-encoded or
cropped copy is not caught (perceptual matching needs the backend).
"""

from datetime import timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from enterprise.models import PhotoFingerprint, ProductRecord
from enterprise.products.common import EvalContext, ProductHandler

WEIGHTS = {
    "price_too_low": 40,
    "price_too_high": 35,
    "unidentified": 30,
    "identification_uncertain": 20,
    "possible_other_identity": 15,
    "duplicate_imagery": 40,
    "seller_pattern": 25,
}
IDENTIFICATION_CHECKS = ("unidentified", "identification_uncertain", "possible_other_identity")
UNCERTAIN_BELOW = 0.6
ALTERNATIVE_AT = 0.2
NOTE = "A risk signal for review, not an authentication or certification."


class VerifySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_low_ratio: float = Field(default=0.5, gt=0, lt=1,
                                   description="Flag a stated value below this share of the low end.")
    price_high_ratio: float = Field(default=1.5, gt=1,
                                    description="Flag a stated value above this multiple of the high end.")
    review_at: int = Field(default=40, ge=1, le=100)
    hold_at: int = Field(default=70, ge=1, le=100)
    seller_lookback_days: int = Field(default=90, ge=1, le=3650)
    seller_flag_count: int = Field(default=2, ge=1,
                                   description="Prior flagged checks for a seller that trigger seller_pattern.")

    @model_validator(mode="after")
    def _ordered(self) -> "VerifySettings":
        if self.review_at >= self.hold_at:
            raise ValueError("review_at must be below hold_at")
        return self


class Subject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["listing", "claim", "appraisal"]
    external_id: str = Field(min_length=1, max_length=128, description="Your id for the listing or claim.")
    seller_id: Optional[str] = Field(default=None, max_length=128,
                                     description="The seller or claimant, for repeat patterns.")
    stated_value: Optional[float] = Field(default=None, gt=0,
                                          description="The asking price, claimed value or appraised value.")
    currency: str = Field(default="USD", min_length=3, max_length=3)


def _iso(value) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _money(value: float, currency: str) -> str:
    return f"${value:,.0f}" if currency == "USD" else f"{value:,.0f} {currency}"


class VerifyHandler(ProductHandler):
    product = "verify"
    object_name = "verify_check"
    id_prefix = "ver"
    result_key = "risk"
    settings_model = VerifySettings

    def evaluate(self, ctx: EvalContext) -> tuple[dict, Optional[str], Optional[int]]:
        cfg = VerifySettings.model_validate(ctx.settings)
        subject = Subject.model_validate(ctx.product_input["subject"])
        ident = ctx.scan["identification"]
        price = ctx.scan["price_context"]
        checks: list[dict] = []

        def check(code: str, fired: bool, message: str, evidence: Optional[dict] = None,
                  ran: bool = True) -> None:
            checks.append({"code": code, "ran": ran, "fired": fired and ran,
                           "weight": WEIGHTS[code] if fired and ran else 0,
                           "message": message, "evidence": evidence or {}})

        # Price checks: only against a scan price in the same currency.
        if subject.stated_value is None:
            for code in ("price_too_low", "price_too_high"):
                check(code, False, "Not run: no stated value was given.", ran=False)
        elif price["status"] != "provided" or price["currency"] != subject.currency:
            reason = ("the scan gave no price" if price["status"] != "provided"
                      else f"the scan price is in {price['currency']}, not {subject.currency}")
            for code in ("price_too_low", "price_too_high"):
                check(code, False, f"Not run: {reason}.", ran=False,
                      evidence={"withheld_reason": price.get("withheld_reason")}
                      if price["status"] != "provided" else {})
        else:
            low, high, cur, stated = price["low"], price["high"], price["currency"], subject.stated_value
            evidence = {"stated_value": stated, "scan_low": low, "scan_high": high, "currency": cur}
            too_low = stated < cfg.price_low_ratio * low
            too_high = stated > cfg.price_high_ratio * high
            check("price_too_low", too_low,
                  f"The stated {_money(stated, cur)} is far below the scan's range of "
                  f"{_money(low, cur)}–{_money(high, cur)}; too cheap often means misdescribed."
                  if too_low else "The stated value is not far below the scan's range.", evidence)
            check("price_too_high", too_high,
                  f"The stated {_money(stated, cur)} is far above the scan's range of "
                  f"{_money(low, cur)}–{_money(high, cur)}; this can mean inflation."
                  if too_high else "The stated value is not far above the scan's range.", evidence)

        # Identification checks.
        confidence = ident.get("confidence")
        unidentified = ident["status"] != "identified"
        check("unidentified", unidentified,
              "The scan could not identify the item." if unidentified else "The scan identified the item.")
        uncertain = not unidentified and confidence is not None and confidence < UNCERTAIN_BELOW
        check("identification_uncertain", uncertain,
              f"The identification is uncertain ({confidence:.0%} confidence)." if uncertain
              else "The identification confidence is acceptable.", {"confidence": confidence})
        likely = [a for a in ident.get("alternatives", []) if a["probability"] >= ALTERNATIVE_AT]
        check("possible_other_identity", bool(likely),
              "It could be something else: " + "; ".join(
                  f"{a['label']} ({a['probability']:.0%})" for a in likely) if likely
              else "No likely alternative identification.", {"alternatives": likely})

        # Duplicate imagery within this account.
        matches = []
        if ctx.photo_hashes:
            rows = ctx.session.scalars(select(PhotoFingerprint).where(
                PhotoFingerprint.tenant_id == ctx.tenant_id,
                PhotoFingerprint.sha256.in_(ctx.photo_hashes),
                PhotoFingerprint.record_id != ctx.record_id,
            ).order_by(PhotoFingerprint.created_at)).all()
            seen: set[str] = set()
            for r in rows:  # one entry per earlier record, however many of its photos match
                if r.reference == subject.external_id or r.record_id in seen:
                    continue
                seen.add(r.record_id)
                matches.append({"record_id": r.record_id, "product": r.product,
                                "reference": r.reference, "seen_at": _iso(r.created_at)})
            matches = matches[:5]
        check("duplicate_imagery", bool(matches),
              f"A photo was already used in this account on {len(matches)} other "
              f"record{'s' if len(matches) != 1 else ''} under another reference." if matches
              else "No reused photos found.",
              {"matches": matches})

        # Seller pattern.
        if subject.seller_id:
            since = ctx.now - timedelta(days=cfg.seller_lookback_days)
            prior = ctx.session.scalar(select(func.count()).select_from(ProductRecord).where(
                ProductRecord.tenant_id == ctx.tenant_id,
                ProductRecord.product == "verify",
                ProductRecord.subject_key == subject.seller_id,
                ProductRecord.id != ctx.record_id,
                ProductRecord.score >= cfg.review_at,
                ProductRecord.created_at >= since,
            )) or 0
            pattern = prior >= cfg.seller_flag_count
            check("seller_pattern", pattern,
                  f"This seller has {prior} flagged checks in the last {cfg.seller_lookback_days} days."
                  if pattern else f"This seller has {prior} flagged checks in the last "
                                  f"{cfg.seller_lookback_days} days.",
                  {"prior_flagged": prior})
        else:
            check("seller_pattern", False, "Not run: no seller_id was given.", ran=False)

        score = min(100, sum(c["weight"] for c in checks))
        fired = {c["code"] for c in checks if c["fired"]}
        if score >= cfg.hold_at:
            action = "hold"
        elif score >= cfg.review_at:
            action = "manual_review"
        elif fired & set(IDENTIFICATION_CHECKS):
            action = "request_lab_report"
        else:
            action = "allow"

        block = {
            "risk_score": score,
            "action": action,
            "checks": checks,
            "subject": subject.model_dump(mode="json"),
            "settings_applied": cfg.model_dump(mode="json"),
            "note": NOTE,
        }
        return block, action, score
