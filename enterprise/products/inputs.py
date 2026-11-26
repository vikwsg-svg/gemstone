"""How a product request becomes a scan request plus the product's own input.

Shared by the HTTP routes and by batch processing, so one row of a CSV and one
API call produce exactly the same record.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel

from enterprise.products.common import ProductHandler, scan_request_from
from enterprise.products.intake import IntakeHandler
from enterprise.products.schemas import IntakeItemRequest, VerifyCheckRequest
from enterprise.products.verify import VerifyHandler
from enterprise.schemas import ItemDetails, ScanRequest

INTAKE, VERIFY = IntakeHandler(), VerifyHandler()
HANDLERS: dict[str, ProductHandler] = {"intake": INTAKE, "verify": VERIFY}
REQUEST_MODELS: dict[str, type[BaseModel]] = {"intake": IntakeItemRequest,
                                              "verify": VerifyCheckRequest}


@dataclass
class Prepared:
    scan_request: ScanRequest
    product_input: dict
    reference: Optional[str]
    subject_key: Optional[str]


def prepare_intake(payload: IntakeItemRequest) -> Prepared:
    details = payload.details or ItemDetails()
    if payload.consignor_description and not details.notes:
        details = details.model_copy(update={"notes": f"Consignor says: {payload.consignor_description}"})
    return Prepared(
        scan_request=scan_request_from(payload.photos, details, payload.model_version,
                                       payload.reference),
        product_input={"consignor_description": payload.consignor_description,
                       "details": payload.details.hints() if payload.details else {}},
        reference=payload.reference,
        subject_key=None,
    )


def prepare_verify(payload: VerifyCheckRequest) -> Prepared:
    subject = payload.subject
    return Prepared(
        scan_request=scan_request_from(payload.photos, payload.details, payload.model_version,
                                       subject.external_id),
        product_input={"subject": subject.model_dump(mode="json")},
        reference=subject.external_id,
        subject_key=subject.seller_id,
    )


def prepare(product: str, payload: BaseModel) -> Prepared:
    return prepare_intake(payload) if product == "intake" else prepare_verify(payload)
