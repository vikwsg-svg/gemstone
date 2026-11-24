"""
CSV batch scans (portfolio revaluation, intake queues).

Upload a CSV; a job is created and processed asynchronously. Each row
becomes a normal persisted, metered scan linked to the job. Rows that
fail validation are reported in the results CSV rather than failing the job.

Input columns (header row required, case-insensitive, extra columns ignored):
    row_id, photo_urls (up to 5 https links separated by "|"),
    item_type (default loose_stone when the row has no photos), valuation_basis
    center or loose stone: variety, species, carat, shape, color_grade, clarity,
        cut_grade, treatment, origin, growth, lab, lab_report_number,
        length_mm, width_mm, depth_mm
    side stones: side_variety (default diamond), side_carat_total, side_count, side_growth
    metal: metal_type, metal_purity, metal_color, metal_weight_grams
    piece: brand, signed, era, condition
The header must include at least one of photo_urls, variety, item_type or metal_type.
``valuation_basis`` per row overrides the job-level default.

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
from enterprise.scan import ScanService, format_validation_error
from enterprise.schemas import ScanRequest

logger = logging.getLogger(__name__)

_STONE_COLUMNS = (
    "variety", "species", "carat", "shape", "color_grade", "clarity", "cut_grade", "treatment",
    "origin", "growth", "lab", "lab_report_number",
)
_SIDE_COLUMNS = ("side_variety", "side_carat_total", "side_count", "side_growth")
_METAL_COLUMNS = ("metal_type", "metal_purity", "metal_color", "metal_weight_grams")
_PIECE_COLUMNS = ("brand", "signed", "era", "condition")
INPUT_COLUMNS = (
    "row_id", "photo_urls", "item_type", *_STONE_COLUMNS, "length_mm", "width_mm", "depth_mm",
    *_SIDE_COLUMNS, *_METAL_COLUMNS, *_PIECE_COLUMNS, "valuation_basis",
)
IDENTIFYING_COLUMNS = ("photo_urls", "variety", "item_type", "metal_type")
RESULT_COLUMNS = (
    "row_number", "row_id", "status", "scan_id", "identified_as", "identification_confidence",
    "item_type", "currency", "valuation_basis",
    "per_carat_low", "per_carat_mid", "per_carat_high", "total_low", "total_mid", "total_high",
    "breakdown_raw", "breakdown_make", "breakdown_market",
    "confidence_score", "confidence_level", "reason_codes", "comps_used", "realized_comps",
    "asking_comps", "model_version", "synthetic_data", "error",
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
    if not any(c in columns for c in IDENTIFYING_COLUMNS):
        raise ApiError(
            422,
            "invalid_csv",
            "The CSV header must include at least one of: " + ", ".join(IDENTIFYING_COLUMNS) + ".",
            details={"accepted": list(INPUT_COLUMNS)},
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
        payload = _row_payload(values)
        basis = values.get("valuation_basis") or default_basis
        if basis:
            payload["valuation_basis"] = basis
        if model_version:
            payload["model_version"] = model_version
        if row_id:
            payload["client_reference"] = row_id
        try:
            request = ScanRequest(**payload)
            rows.append(BatchRow(row_number, row_id, request, None))
        except ValidationError as exc:
            rows.append(BatchRow(row_number, row_id, None, format_validation_error(exc)))
    return rows


def _row_payload(values: dict[str, str]) -> dict:
    """Map one CSV row onto the ValuationRequest shape."""
    payload: dict = {}
    urls = [u.strip() for u in (values.get("photo_urls") or "").split("|") if u.strip()]
    if urls:
        payload["photos"] = [{"url": u} for u in urls]
    item_type = values.get("item_type") or (None if urls else "loose_stone")
    if item_type:
        payload["item_type"] = item_type
    stones = []
    stone = {k: values[k] for k in _STONE_COLUMNS if values.get(k)}
    if stone:
        stone["role"] = "center"
        if values.get("length_mm") or values.get("width_mm") or values.get("depth_mm"):
            stone["dimensions_mm"] = {
                k: values[f"{k}_mm"] for k in ("length", "width", "depth") if values.get(f"{k}_mm")
            }
        stones.append(stone)
    if values.get("side_variety") or values.get("side_carat_total"):
        side = {"role": "side", "variety": values.get("side_variety") or "diamond",
                "carat": values.get("side_carat_total"), "count": values.get("side_count") or 1}
        if values.get("side_growth"):
            side["growth"] = values["side_growth"]
        stones.append(side)
    if stones:
        payload["stones"] = stones
    if values.get("metal_type"):
        metal = {"type": values["metal_type"]}
        for col, key in (("metal_purity", "purity"), ("metal_color", "color"),
                         ("metal_weight_grams", "weight_grams")):
            if values.get(col):
                metal[key] = values[col]
        payload["metal"] = metal
    for col in _PIECE_COLUMNS:
        if values.get(col):
            payload[col] = values[col]
    return payload


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
    ident = body.get("identification") or {}
    summary = body.get("comps_summary") or {}
    confidence = body.get("confidence") or {}
    out.update(
        status=body["status"],
        scan_id=body["scan_id"],
        identified_as=_safe_cell(ident.get("summary")),
        identification_confidence=ident.get("confidence") if ident.get("confidence") is not None else "",
        item_type=body.get("item_type") or "",
        currency=body["currency"],
        valuation_basis=body["valuation_basis"],
        confidence_score=confidence.get("score", ""),
        confidence_level=confidence.get("level", ""),
        reason_codes="|".join(r["code"] for r in confidence.get("reasons", [])),
        comps_used=summary.get("used", ""),
        realized_comps=summary.get("realized", ""),
        asking_comps=summary.get("asking", ""),
        model_version=body["model_version"],
        synthetic_data=str(body["synthetic_data"]).lower(),
    )
    price = body.get("price")
    if price:
        for band in ("low", "mid", "high"):
            if price.get("per_carat"):
                out[f"per_carat_{band}"] = price["per_carat"][band]
            out[f"total_{band}"] = price["total"][band]
    if body.get("breakdown"):
        for part in ("raw", "make", "market"):
            out[f"breakdown_{part}"] = body["breakdown"][part]["value"]
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
            service = ScanService(state, session)
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
                            principal, row.request, batch_job_id=job.id, batch_row=row.row_number,
                            wait_seconds=state.settings.identify_timeout_seconds,
                        )
                    except ApiError as exc:
                        session.rollback()
                        error = f"{exc.code}: {exc.message}"
                if body is None:
                    job.failed_rows += 1
                elif body["status"] == "priced":
                    job.priced_rows += 1
                else:  # insufficient_data, unidentified or still processing
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
