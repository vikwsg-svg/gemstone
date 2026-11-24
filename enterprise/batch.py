"""
CSV batch valuations (portfolio revaluation).

Upload a CSV; a job is created and processed asynchronously. Each row
becomes a normal persisted, metered valuation linked to the job. Rows that
fail validation are reported in the results CSV rather than failing the job.

Input columns (header row required, case-insensitive, extra columns ignored):
    row_id, variety*, species, carat*, shape*, color_grade, clarity, treatment,
    origin, lab, lab_report_number, length_mm, width_mm, depth_mm, valuation_basis
(* required). ``valuation_basis`` per row overrides the job-level default.

The whole batch is pinned to one model version, resolved at submission.

v1 runs jobs in-process after the response (FastAPI background task).
``process_batch_job`` is a plain function so it can be moved onto the
existing Celery workers without changes.
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
from enterprise.schemas import ValuationRequest
from enterprise.valuation.service import ValuationService

logger = logging.getLogger(__name__)

INPUT_COLUMNS = (
    "row_id", "variety", "species", "carat", "shape", "color_grade", "clarity", "treatment",
    "origin", "lab", "lab_report_number", "length_mm", "width_mm", "depth_mm",
    "valuation_basis",
)
REQUIRED_COLUMNS = ("variety", "carat", "shape")
_PASSTHROUGH = (
    "variety", "species", "carat", "shape", "color_grade", "clarity", "treatment", "origin",
    "lab", "lab_report_number",
)
RESULT_COLUMNS = (
    "row_number", "row_id", "status", "valuation_id", "currency", "valuation_basis",
    "per_carat_low", "per_carat_mid", "per_carat_high", "total_low", "total_mid", "total_high",
    "confidence_score", "confidence_level", "reason_codes", "comps_used", "realized_comps",
    "asking_comps", "model_version", "synthetic_data", "error",
)


@dataclass
class BatchRow:
    row_number: int
    row_id: Optional[str]
    request: Optional[ValuationRequest]
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
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_rows(
    text: str,
    *,
    default_basis: Optional[str],
    model_version: Optional[str],
    max_rows: int,
) -> list[BatchRow]:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ApiError(422, "invalid_csv", "The CSV is empty.") from None
    columns = [h.strip().lower() for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise ApiError(
            422,
            "invalid_csv",
            f"The CSV is missing required columns: {', '.join(missing)}.",
            details={"required": list(REQUIRED_COLUMNS), "accepted": list(INPUT_COLUMNS)},
        )

    rows: list[BatchRow] = []
    for line in reader:
        if not any(cell.strip() for cell in line):
            continue
        if len(rows) >= max_rows:
            raise ApiError(
                422, "too_many_rows", f"A batch may contain at most {max_rows} rows."
            )
        values = {
            col: line[i].strip()
            for i, col in enumerate(columns)
            if i < len(line) and col in INPUT_COLUMNS
        }
        row_number = len(rows) + 1
        row_id = values.get("row_id") or None
        payload: dict = {k: values[k] for k in _PASSTHROUGH if values.get(k)}
        if values.get("length_mm") or values.get("width_mm") or values.get("depth_mm"):
            payload["dimensions_mm"] = {
                k: values[f"{k}_mm"] for k in ("length", "width", "depth") if values.get(f"{k}_mm")
            }
        basis = values.get("valuation_basis") or default_basis
        if basis:
            payload["valuation_basis"] = basis
        if model_version:
            payload["model_version"] = model_version
        if row_id:
            payload["client_reference"] = row_id
        try:
            request = ValuationRequest(**payload)
            rows.append(BatchRow(row_number, row_id, request, None))
        except ValidationError as exc:
            rows.append(BatchRow(row_number, row_id, None, _format_validation_error(exc)))
    return rows


def _safe_cell(value: Optional[str]) -> str:
    """Neutralize spreadsheet formula injection in user-supplied text."""
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
    out.update(
        status=body["status"],
        valuation_id=body["valuation_id"],
        currency=body["currency"],
        valuation_basis=body["valuation_basis"],
        confidence_score=body["confidence"]["score"],
        confidence_level=body["confidence"]["level"],
        reason_codes="|".join(r["code"] for r in body["confidence"]["reasons"]),
        comps_used=body["comps_summary"]["used"],
        realized_comps=body["comps_summary"]["realized"],
        asking_comps=body["comps_summary"]["asking"],
        model_version=body["model_version"],
        synthetic_data=str(body["synthetic_data"]).lower(),
    )
    price = body.get("price")
    if price:
        for band in ("low", "mid", "high"):
            out[f"per_carat_{band}"] = price["per_carat"][band]
            out[f"total_{band}"] = price["total"][band]
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
            principal = Principal(
                tenant=session.get(Tenant, job.tenant_id),
                api_key=session.get(ApiKey, job.api_key_id),
            )
            service = ValuationService(state, session)
            rows = parse_rows(
                job.input_csv,
                default_basis=job.valuation_basis,
                model_version=job.model_version,
                max_rows=state.settings.batch_max_rows,
            )
            results = []
            for row in rows:
                body, error = None, row.error
                if row.request is not None:
                    try:
                        body = service.create(
                            principal, row.request, batch_job_id=job.id, batch_row=row.row_number
                        )
                    except ApiError as exc:
                        session.rollback()
                        error = f"{exc.code}: {exc.message}"
                if body is None:
                    job.failed_rows += 1
                elif body["status"] == "priced":
                    job.priced_rows += 1
                else:
                    job.insufficient_rows += 1
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
