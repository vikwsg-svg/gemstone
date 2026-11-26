"""
CSV batch scans (portfolio revaluation, intake queues).

Upload a CSV; a job is created and processed asynchronously. Each row becomes
a normal stored, metered scan linked to the job. Rows that fail validation are
reported in the results CSV rather than failing the job.

Columns (header row required, case-insensitive, extra columns ignored):
    photo_urls (required: 1-5 https links separated by "|")
    row_id, item_type, metal_type, metal_purity, metal_weight_grams, brand,
    signed, era, condition, lab, lab_report_number, notes (optional details)

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
from enterprise.models import ApiKey, BatchJob, Tenant
from enterprise.scan import ScanService
from enterprise.schemas import ScanRequest

logger = logging.getLogger(__name__)

_DETAIL_COLUMNS = ("item_type", "brand", "signed", "era", "condition", "lab",
                   "lab_report_number", "notes")
_METAL_COLUMNS = {"metal_type": "type", "metal_purity": "purity",
                  "metal_weight_grams": "weight_grams"}
INPUT_COLUMNS = ("row_id", "photo_urls", *_DETAIL_COLUMNS, *_METAL_COLUMNS)
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
    request: Optional[ScanRequest]
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


def _row_payload(values: dict[str, str], model_version: Optional[str]) -> dict:
    urls = [u.strip() for u in (values.get("photo_urls") or "").split("|") if u.strip()]
    payload: dict = {"photos": [{"url": u} for u in urls]}
    details = {k: values[k] for k in _DETAIL_COLUMNS if values.get(k)}
    metal = {key: values[col] for col, key in _METAL_COLUMNS.items() if values.get(col)}
    if metal:
        details["metal"] = metal
    if details:
        payload["details"] = details
    if values.get("row_id"):
        payload["client_reference"] = values["row_id"]
    if model_version:
        payload["model_version"] = model_version
    return payload


def parse_rows(text: str, *, model_version: Optional[str], max_rows: int) -> list[BatchRow]:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ApiError(422, "invalid_csv", "The CSV is empty.") from None
    columns = [h.strip().lower() for h in header]
    if "photo_urls" not in columns:
        raise ApiError(422, "invalid_csv", "The CSV must have a photo_urls column.",
                       details={"accepted": list(INPUT_COLUMNS)})

    rows: list[BatchRow] = []
    for line in reader:
        if not any(cell.strip() for cell in line):
            continue
        if len(rows) >= max_rows:
            raise ApiError(422, "too_many_rows", f"A batch may contain at most {max_rows} rows.")
        values = {col: line[i].strip() for i, col in enumerate(columns)
                  if i < len(line) and col in INPUT_COLUMNS}
        row_number = len(rows) + 1
        row_id = values.get("row_id") or None
        try:
            request = ScanRequest(**_row_payload(values, model_version))
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


def build_results_csv(results: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(RESULT_COLUMNS), lineterminator="\n")
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
            service = ScanService(state, session)
            rows = parse_rows(job.input_csv, model_version=job.model_version,
                              max_rows=state.settings.batch_max_rows)
            results = []
            for row in rows:
                body, error = None, row.error
                if row.request is not None:
                    try:
                        body = service.create(principal, row.request, batch_job_id=job.id,
                                              batch_row=row.row_number,
                                              wait_seconds=state.settings.scan_timeout_seconds)
                    except ApiError as exc:
                        session.rollback()
                        error = f"{exc.code}: {exc.message}"
                if body is None:
                    job.failed_rows += 1
                elif body["price_context"]["status"] == "provided":
                    job.priced_rows += 1
                else:  # withheld, or still processing
                    job.unpriced_rows += 1
                job.processed_rows += 1
                results.append(_result_row(row, body, error))
                session.commit()

            job.results_csv = build_results_csv(results)
            job.status = "completed"
            job.completed_at = state.clock()
            session.commit()
        except Exception:  # noqa: BLE001 - a job must always reach a terminal state
            logger.exception("batch job %s failed", job_id)
            session.rollback()
            job = session.get(BatchJob, job_id)
            job.status = "failed"
            job.error = "The batch failed unexpectedly. Contact support with this job id."
            job.completed_at = state.clock()
            session.commit()
