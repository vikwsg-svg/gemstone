"""HTTP routes for Index (scope ``index``). Read-only: levels are published by operators."""

import datetime as dt
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from enterprise.auth import Principal, require_scope
from enterprise.deps import AppState, get_db, get_state
from enterprise.errors import ApiError
from enterprise.index import PERIOD_RE, SERIES, IndexService, to_csv
from enterprise.schemas import ErrorResponse

router = APIRouter()

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API key."},
    403: {"model": ErrorResponse, "description": "Key lacks the `index` scope."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
_SANDBOX_DOC = "true for sandbox keys: generated example series, not market data."


class IndexPoint(BaseModel):
    period: str = Field(description="Month, YYYY-MM.")
    level: float
    comp_count: int = Field(description="Comparable sales behind this value.")
    thin: bool = Field(description="Fewer comparables than `thin_below`: treat with caution.")
    change_mom_pct: Optional[float] = Field(description="Change from the previous month, in %.")
    change_yoy_pct: Optional[float] = Field(description="Change from the same month last year, in %.")
    revised: bool = Field(description="This value was corrected after first publication.")
    published_at: dt.datetime


class SeriesSummary(BaseModel):
    key: str
    name: str
    description: str
    methodology_version: Optional[str]
    first_period: Optional[str]
    latest: Optional[IndexPoint]


class SeriesList(BaseModel):
    object: Literal["list"] = "list"
    synthetic_data: bool = Field(description=_SANDBOX_DOC)
    data: list[SeriesSummary]


class SeriesResponse(BaseModel):
    object: Literal["index_series"] = "index_series"
    key: str
    name: str
    description: str
    methodology_version: Optional[str]
    thin_below: int
    synthetic_data: bool = Field(description=_SANDBOX_DOC)
    data: list[IndexPoint]


class Correction(BaseModel):
    series_key: str
    period: str
    old_level: float
    new_level: float
    change_pct: Optional[float]
    reason: str
    published_at: dt.datetime


class CorrectionList(BaseModel):
    object: Literal["list"] = "list"
    synthetic_data: bool = Field(description=_SANDBOX_DOC)
    data: list[Correction]


def _service(principal: Principal, db: Session, state: AppState) -> IndexService:
    return IndexService(db, principal.tenant.environment == "sandbox", state.clock())


def _period(value: Optional[str], name: str) -> Optional[str]:
    if value is not None and not PERIOD_RE.match(value):
        raise ApiError(422, "invalid_period", f"{name} must be a month in the form YYYY-MM.")
    return value


def _known(key: str) -> str:
    if key not in SERIES:
        raise ApiError(404, "series_not_found", f"No index series {key!r}.",
                       details={"known_series": list(SERIES)})
    return key


@router.get("/index/series", response_model=SeriesList, responses=_ERRORS, tags=["Index"],
            summary="List the index series with their latest values")
def list_series(principal: Principal = Depends(require_scope("index")),
                db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> SeriesList:
    service = _service(principal, db, state)
    return SeriesList(synthetic_data=service.sandbox, data=service.list_series())


@router.get(
    "/index/series/{series_key}", response_model=SeriesResponse,
    responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "Unknown series."},
               200: {"content": {"text/csv": {}},
                     "description": "The series as JSON, or as CSV with `format=csv`."}},
    tags=["Index"], summary="Monthly levels for one series",
    description="Monthly levels with comparable counts, a thin-data flag, month-on-month and "
                "year-on-year change, and whether each value was revised. `format=csv` returns "
                "the same columns as a CSV file.",
)
def get_series(
    series_key: str = Path(max_length=64),
    start: Optional[str] = Query(default=None, description="First month, YYYY-MM."),
    end: Optional[str] = Query(default=None, description="Last month, YYYY-MM."),
    format: Literal["json", "csv"] = Query(default="json"),
    principal: Principal = Depends(require_scope("index")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
):
    key = _known(series_key)
    start, end = _period(start, "start"), _period(end, "end")
    if start and end and start > end:
        raise ApiError(422, "invalid_period", "start must be on or before end.")
    service = _service(principal, db, state)
    body = service.series(key, start, end)
    if format == "csv":
        return PlainTextResponse(to_csv(body["data"]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{key}.csv"'})
    return SeriesResponse(synthetic_data=service.sandbox, **{k: v for k, v in body.items()
                                                             if k != "object"})


@router.get("/index/corrections", response_model=CorrectionList, responses=_ERRORS, tags=["Index"],
            summary="Corrections to published values",
            description="Published values are never overwritten. Each correction is listed here "
                        "with the old and new level and the reason.")
def list_corrections(
    series: Optional[str] = Query(default=None, max_length=64, description="Only this series."),
    since: Optional[dt.date] = Query(default=None, description="Only corrections published on or after this date."),
    principal: Principal = Depends(require_scope("index")),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> CorrectionList:
    if series is not None:
        _known(series)
    service = _service(principal, db, state)
    return CorrectionList(synthetic_data=service.sandbox, data=service.corrections(series, since))
