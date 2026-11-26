"""
CSV batch for every product: portfolio revaluation (Scan API), back-of-house
consignment queues (Intake) and nightly listing or claim screens (Verify).

Upload a CSV; a job is created and processed asynchronously. Each row becomes
a normal stored, metered record linked to the job, exactly as if it had been
sent to the single-item endpoint. Rows that fail validation are reported in the
results CSV rather than failing the job.

Columns (header row required, case-insensitive, extra columns ignored):
    photo_urls (required: 1-5 https links separated by "|")
    row_id, item_type, metal_type, metal_purity, metal_weight_grams, brand,
    signed, era, condition, lab, lab_report_number, notes (optional details)
  Intake also: reference (default row_id), consignor_description
  Verify also: external_id (default row_id), subject_type (listing | claim |
    appraisal, default listing), seller_id, stated_value, currency

v1 runs jobs in-process after the response (FastAPI background task).
``process_batch_job`` is a plain function so it can move onto the existing
Celery workers without changes.
"""

import csv
import io
import logging
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError

from enterprise.auth import Principal
from enterprise.deps import AppState
from enterprise.errors import ApiError
from enterprise.models import ApiKey, BatchJob, ProductRecord, Tenant
from enterprise.scan import ScanService
from enterprise.schemas import ScanRequest
from enterprise.webhooks import dispatch_pending, emit

logger = logging.getLogger(__name__)

_DETAIL_COLUMNS = ("item_type", "brand", "signed", "era", "condition", "lab",
                   "lab_report_number", "notes")
_METAL_COLUMNS = {"metal_type": "type", "metal_purity": "purity",
                  "metal_weight_grams": "weight_grams"}
INPUT_COLUMNS = ("row_id", "photo_urls", *_DETAIL_COLUMNS, *_METAL_COLUMNS)
PRODUCT_COLUMNS = {
    "scan": (),
    "intake": ("reference", "consignor_description"),
    "verify": ("external_id", "subject_type", "seller_id", "stated_value", "currency"),
}
RESULT_COLUMNS = (
    "row_number", "row_id", "status", "scan_id", "identified_as", "identification_confidence",
    "price_status", "price_low", "price_high", "currency", "price_basis", "withheld_reason",
    "comparables", "realized_comparables", "asking_comparables", "model_version",
    "synthetic_data", "error",
)


@dataclass
class BatchRow:
    row_number: int
    row_id: Optional[str]
    request: Optional[object]  # ScanRequest, IntakeItemRequest or VerifyCheckRequest
    error: Optional[str]


def decode_csv(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError(422, "invalid_csv", "The file must be a UTF-8 encoded CSV.") from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else err.get("msg", "invalid"))
    return "; ".join(parts)


def _row_payload(values: dict[str, str], model_version: Optional[str], product: str) -> dict:
    urls = [u.strip() for u in (values.get("photo_urls") or "").split("|") if u.strip()]
    payload: dict = {"photos": [{"url": u} for u in urls]}
    details = {k: values[k] for k in _DETAIL_COLUMNS if values.get(k)}
    metal = {key: values[col] for col, key in _METAL_COLUMNS.items() if values.get(col)}
    if metal:
        details["metal"] = metal
    if details:
        payload["details"] = details
    if model_version:
        payload["model_version"] = model_version
    row_id = values.get("row_id") or None
    if product == "scan":
        if row_id:
            payload["client_reference"] = row_id
    elif product == "intake":
        payload["reference"] = values.get("reference") or row_id
        if values.get("consignor_description"):
            payload["consignor_description"] = values["consignor_description"]
    else:
        subject = {"type": values.get("subject_type") or "listing",
                   "external_id": values.get("external_id") or row_id}
        for key in ("seller_id", "stated_value", "currency"):
            if values.get(key):
                subject[key] = values[key]
        payload["subject"] = subject
    return {k: v for k, v in payload.items() if v is not None}


def _request_model(product: str):
    if product == "scan":
        return ScanRequest
    from enterprise.products.inputs import REQUEST_MODELS  # avoid an import cycle
    return REQUEST_MODELS[product]


def parse_rows(text: str, *, model_version: Optional[str], max_rows: int,
               product: str = "scan") -> list[BatchRow]:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ApiError(422, "invalid_csv", "The CSV is empty.") from None
    columns = [h.strip().lower() for h in header]
    accepted = (*INPUT_COLUMNS, *PRODUCT_COLUMNS[product])
    if "photo_urls" not in columns:
        raise ApiError(422, "invalid_csv", "The CSV must have a photo_urls column.",
                       details={"accepted": list(accepted)})
    model = _request_model(product)

    rows: list[BatchRow] = []
    for line in reader:
        if not any(cell.strip() for cell in line):
            continue
        if len(rows) >= max_rows:
            raise ApiError(422, "too_many_rows", f"A batch may contain at most {max_rows} rows.")
        values = {col: line[i].strip() for i, col in enumerate(columns)
                  if i < len(line) and col in accepted}
        row_number = len(rows) + 1
        row_id = values.get("row_id") or None
        try:
            request = model(**_row_payload(values, model_version, product))
            rows.append(BatchRow(row_number, row_id, request, None))
        except ValidationError as exc:
            rows.append(BatchRow(row_number, row_id, None, _format_validation_error(exc)))
    return rows


def _safe_cell(value: Optional[str]) -> str:
    """Neutralize spreadsheet formula injection in text that came from users or engines."""
    if not value:
        return ""
    return "'" + value if value[0] in ("=", "+", "-", "@", "\t", "\r") else value


def _result_row(row: BatchRow, body: Optional[dict], error: Optional[str]) -> dict:
    out = {col: "" for col in RESULT_COLUMNS}
    out["row_number"] = row.row_number
    out["row_id"] = _safe_cell(row.row_id)
    if body is None:
        out["status"] = "error"
        out["error"] = _safe_cell(error)
        return out
    ident = body["identification"]
    price = body["price_context"]
    comps = body["comparables"]
    out.update(
        status=body["status"],
        scan_id=body["scan_id"],
        identified_as=_safe_cell(ident.get("summary")),
        identification_confidence=ident.get("confidence") if ident.get("confidence") is not None else "",
        price_status=price["status"],
        price_low=price.get("low") if price.get("low") is not None else "",
        price_high=price.get("high") if price.get("high") is not None else "",
        currency=price.get("currency", ""),
        price_basis=_safe_cell(price.get("basis")),
        withheld_reason=_safe_cell(price.get("withheld_reason")),
        comparables=comps.get("total", ""),
        realized_comparables=comps.get("realized", ""),
        asking_comparables=comps.get("asking", ""),
        model_version=body.get("model_version") or "",
        synthetic_data=str(body["synthetic_data"]).lower(),
    )
    return out


def product_result_columns(service) -> list[str]:
    return ["row_number", "row_id", "row_status", "error", *service.columns(), "synthetic_data"]


def _product_result_row(service, row: BatchRow, record: Optional[ProductRecord],
                        body: Optional[dict], error: Optional[str]) -> dict:
    out = {"row_number": row.row_number, "row_id": _safe_cell(row.row_id)}
    if record is None:
        out.update(row_status="error", error=_safe_cell(error))
        return out
    summary = service.summary(record)
    for key, value in summary.items():
        if isinstance(value, list):
            value = "|".join(value)
        elif isinstance(value, bool):
            value = str(value).lower()
        out[key] = _safe_cell(value) if isinstance(value, str) else ("" if value is None else value)
    out.update(row_status=record.status,
               synthetic_data=str(body["scan"]["synthetic_data"]).lower())
    return out


def build_results_csv(results: list[dict], columns=RESULT_COLUMNS) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n", restval="")
    writer.writeheader()
    writer.writerows(results)
    return buf.getvalue()


def process_batch_job(state: AppState, job_id: str) -> None:
    with state.db.session_scope() as session:
        job = session.get(BatchJob, job_id)
        if job is None or job.status != "queued":
            return
        job.status = "running"
        job.started_at = state.clock()
        session.commit()

        try:
            principal = Principal(tenant=session.get(Tenant, job.tenant_id),
                                  api_key=session.get(ApiKey, job.api_key_id))
            product = job.product or "scan"
            if product == "scan":
                service, columns = ScanService(state, session), RESULT_COLUMNS
            else:
                from enterprise.products.common import ProductService
                from enterprise.products.inputs import HANDLERS, prepare
                service = ProductService(state, session, HANDLERS[product])
                columns = product_result_columns(service)
            rows = parse_rows(job.input_csv, model_version=job.model_version,
                              max_rows=state.settings.batch_max_rows, product=product)
            wait = state.settings.scan_timeout_seconds
            results = []
            for row in rows:
                body, record, error = None, None, row.error
                if row.request is not None:
                    try:
                        if product == "scan":
                            body = service.create(principal, row.request, batch_job_id=job.id,
                                                  batch_row=row.row_number, wait_seconds=wait)
                        else:
                            prepared = prepare(product, row.request)
                            body = service.create(
                                principal, prepared.scan_request, prepared.product_input,
                                reference=prepared.reference, subject_key=prepared.subject_key,
                                batch_job_id=job.id, batch_row=row.row_number, wait_seconds=wait)
                            record = session.get(ProductRecord, body["id"])
                    except ApiError as exc:
                        session.rollback()
                        error = f"{exc.code}: {exc.message}"
                price = (body or {}).get("price_context") or ((body or {}).get("scan") or {}).get(
                    "price_context") or {}
                if body is None:
                    job.failed_rows += 1
                elif price.get("status") == "provided":
                    job.priced_rows += 1
                else:  # withheld, or still processing
                    job.unpriced_rows += 1
                job.processed_rows += 1
                results.append(_result_row(row, body, error) if product == "scan"
                               else _product_result_row(service, row, record, body, error))
                session.commit()

            job.results_csv = build_results_csv(results, columns)
            job.status = "completed"
            job.completed_at = state.clock()
            emit(session, job.tenant_id, "batch.completed", {
                "object": "batch_job", "id": job.id, "product": product,
                "total_rows": job.total_rows, "processed_rows": job.processed_rows,
                "failed_rows": job.failed_rows}, state.clock())
            session.commit()
        except Exception:  # noqa: BLE001 - a job must always reach a terminal state
            logger.exception("batch job %s failed", job_id)
            session.rollback()
            job = session.get(BatchJob, job_id)
            job.status = "failed"
            job.error = "The batch failed unexpectedly. Contact support with this job id."
            job.completed_at = state.clock()
            session.commit()
        tenant_id = job.tenant_id
    dispatch_pending(state, tenant_id)  # webhooks for the rows and the batch, after commit
