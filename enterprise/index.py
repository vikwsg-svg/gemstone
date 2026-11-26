"""
Index: the GemstoneAI Price Index (GPI) and its sub-indices as a licensed data feed.

This module does not calculate the index. The existing GPI calculation
publishes monthly levels into ``ent_index_snapshots`` (``enterprise.cli
index-publish``); the API serves them. What the enterprise layer adds:

- one series catalog (``SERIES``) with stable keys;
- comparable counts on every value, with a ``thin`` flag below
  ``THIN_BELOW`` so weak cells are visible;
- month-on-month and year-on-year change from one function (``change_pct``);
- immutability: a published value is never edited. A correction adds a new
  current row plus an ``IndexCorrection`` entry, and points that were
  corrected are marked ``revised``;
- sandbox keys get generated example series (``synthetic_data: true``), so
  integrations can be built before a license is signed.

Index calls are not metered: Index is licensed as a feed, not billed per call.
"""

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from enterprise.models import IndexCorrection, IndexSnapshot

THIN_BELOW = 30
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SANDBOX_METHODOLOGY = "sandbox-example"
SANDBOX_START = "2023-01"


@dataclass(frozen=True)
class Series:
    key: str
    name: str
    description: str


SERIES: dict[str, Series] = {s.key: s for s in (
    Series("gpi", "GemstoneAI Price Index",
           "The headline index: all jewelry, diamonds and gemstones in GemstoneAI's comparable data."),
    Series("diamonds-natural", "Natural diamonds", "Loose and set natural diamonds."),
    Series("diamonds-lab-grown", "Lab-grown diamonds", "Loose and set laboratory-grown diamonds."),
    Series("colored-gemstones", "Colored gemstones",
           "Sapphire, ruby, emerald and other colored gemstones, loose and set."),
    Series("signed-jewelry", "Signed jewelry", "Pieces by recognized makers and houses."),
    Series("period-jewelry", "Period jewelry", "Antique and vintage jewelry."),
    Series("gold-platinum", "Gold and platinum pieces",
           "Jewelry valued mainly for its metal and workmanship."),
)}

CSV_COLUMNS = ["period", "level", "comp_count", "thin", "change_mom_pct", "change_yoy_pct",
               "revised", "published_at"]


class IndexPublishError(ValueError):
    """A publish or correction request that cannot be applied."""


def change_pct(current: float, previous: Optional[float]) -> Optional[float]:
    """Percentage change, rounded to 2 places; None without a usable previous value."""
    if previous is None or previous <= 0:
        return None
    return round((current / previous - 1) * 100, 2)


def shift_period(period: str, months: int) -> str:
    year, month = int(period[:4]), int(period[5:7])
    total = year * 12 + (month - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def last_complete_period(now: datetime) -> str:
    return shift_period(f"{now.year:04d}-{now.month:02d}", -1)


def _point(period: str, level: float, comp_count: int, published_at: datetime,
           revised: bool) -> dict:
    return {"period": period, "level": round(level, 2), "comp_count": comp_count,
            "thin": comp_count < THIN_BELOW, "change_mom_pct": None, "change_yoy_pct": None,
            "revised": revised, "published_at": _aware(published_at).isoformat()}


def _with_changes(points: list[dict]) -> list[dict]:
    """Add MoM and YoY change. Needs the exact previous month / same month last year."""
    by_period = {p["period"]: p["level"] for p in points}
    for p in points:
        p["change_mom_pct"] = change_pct(p["level"], by_period.get(shift_period(p["period"], -1)))
        p["change_yoy_pct"] = change_pct(p["level"], by_period.get(shift_period(p["period"], -12)))
    return points


# ── Sandbox: generated example series ────────────────────────────────────────


def _unit(*parts: str) -> float:
    """A deterministic number in [0, 1) from the given strings."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


# Typical monthly comparable counts per sandbox sub-index. Period jewelry is
# deliberately small so some of its months are flagged thin.
SANDBOX_COMPS = {"diamonds-natural": 900, "diamonds-lab-grown": 700, "colored-gemstones": 500,
                 "signed-jewelry": 160, "period-jewelry": 45, "gold-platinum": 300}


def _sandbox_component(key: str, now: datetime) -> list[tuple[str, float, int]]:
    trend = (_unit(key, "trend") - 0.25) * 0.008  # about -0.2% to +0.6% a month
    season = 0.5 + _unit(key, "season") * 2.0
    out, level, period, end = [], 100.0, SANDBOX_START, last_complete_period(now)
    while period <= end:
        if out:
            level *= 1 + trend + (_unit(key, period, "noise") - 0.5) * 0.02
        seasonal = season * math.sin(2 * math.pi * (int(period[5:7]) - 3) / 12)
        comps = max(5, int(SANDBOX_COMPS[key] * (0.6 + 0.8 * _unit(key, period, "n"))))
        out.append((period, level + seasonal, comps))
        period = shift_period(period, 1)
    return out


def _sandbox_points(key: str, now: datetime) -> list[dict]:
    """Example series, base 100 = January 2023. The headline GPI is the
    comparable-weighted mean of the sub-indices, so the example data agrees with itself."""
    if key == "gpi":
        parts = [_sandbox_component(k, now) for k in SANDBOX_COMPS]
        rows = []
        for month in zip(*parts):
            total = sum(c for _, _, c in month)
            rows.append((month[0][0], sum(level * c for _, level, c in month) / total, total))
    else:
        rows = _sandbox_component(key, now)
    points = []
    for period, level, comps in rows:
        year, month = int(period[:4]), int(period[5:7])
        published = datetime(year + (month == 12), month % 12 + 1, 3, tzinfo=timezone.utc)
        points.append(_point(period, level, comps, published, False))
    return points


# ── Service ──────────────────────────────────────────────────────────────────


class IndexService:
    def __init__(self, session: Session, sandbox: bool, now: datetime):
        self.session = session
        self.sandbox = sandbox
        self.now = now

    def _live_points(self, key: str) -> tuple[list[dict], Optional[str]]:
        rows = self.session.scalars(select(IndexSnapshot).where(
            IndexSnapshot.series_key == key, IndexSnapshot.is_current.is_(True),
        ).order_by(IndexSnapshot.period)).all()
        revised = set(self.session.scalars(select(IndexCorrection.period).where(
            IndexCorrection.series_key == key)).all())
        points = [_point(r.period, r.level, r.comp_count, r.published_at, r.period in revised)
                  for r in rows]
        return points, (rows[-1].methodology_version if rows else None)

    def points(self, key: str) -> tuple[list[dict], Optional[str]]:
        if self.sandbox:
            return _with_changes(_sandbox_points(key, self.now)), SANDBOX_METHODOLOGY
        points, methodology = self._live_points(key)
        return _with_changes(points), methodology

    def list_series(self) -> list[dict]:
        out = []
        for s in SERIES.values():
            points, methodology = self.points(s.key)
            out.append({"key": s.key, "name": s.name, "description": s.description,
                        "methodology_version": methodology,
                        "first_period": points[0]["period"] if points else None,
                        "latest": points[-1] if points else None})
        return out

    def series(self, key: str, start: Optional[str], end: Optional[str]) -> dict:
        s = SERIES[key]
        points, methodology = self.points(key)
        window = [p for p in points
                  if (start is None or p["period"] >= start) and (end is None or p["period"] <= end)]
        return {"object": "index_series", "key": s.key, "name": s.name,
                "description": s.description, "methodology_version": methodology,
                "thin_below": THIN_BELOW, "data": window}

    def corrections(self, key: Optional[str], since: Optional[date]) -> list[dict]:
        if self.sandbox:
            return []
        query = select(IndexCorrection).order_by(IndexCorrection.published_at, IndexCorrection.id)
        if key:
            query = query.where(IndexCorrection.series_key == key)
        rows = self.session.scalars(query).all()
        return [{"series_key": c.series_key, "period": c.period, "old_level": round(c.old_level, 2),
                 "new_level": round(c.new_level, 2), "change_pct": change_pct(c.new_level, c.old_level),
                 "reason": c.reason, "published_at": _aware(c.published_at).isoformat()}
                for c in rows
                if since is None or _aware(c.published_at).date() >= since]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def to_csv(points: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for p in points:
        writer.writerow({k: ("" if p[k] is None else str(p[k]).lower() if isinstance(p[k], bool)
                             else p[k]) for k in CSV_COLUMNS})
    return buffer.getvalue()


# ── Publishing (operator side) ───────────────────────────────────────────────


def _current(session: Session, key: str, period: str) -> Optional[IndexSnapshot]:
    return session.scalars(select(IndexSnapshot).where(
        IndexSnapshot.series_key == key, IndexSnapshot.period == period,
        IndexSnapshot.is_current.is_(True))).first()


def _validate(key: str, period: str, level: float, comp_count: int, now: datetime) -> None:
    if key not in SERIES:
        raise IndexPublishError(f"unknown series {key!r}; known: {', '.join(SERIES)}")
    if not PERIOD_RE.match(period):
        raise IndexPublishError(f"period {period!r} must be YYYY-MM")
    if period > last_complete_period(now):
        raise IndexPublishError(f"period {period} is not complete yet")
    if not (isinstance(level, (int, float)) and math.isfinite(level) and level > 0):
        raise IndexPublishError(f"{key} {period}: level must be a positive number")
    if comp_count < 0:
        raise IndexPublishError(f"{key} {period}: comp_count must not be negative")


def publish(session: Session, rows: list[dict], now: datetime) -> dict:
    """Publish levels. All-or-nothing; re-publishing an identical value is a no-op.

    Each row: series_key, period, level, comp_count, methodology_version.
    A different value for an already-published period is refused: use ``correct``.
    """
    staged, skipped = [], 0
    seen = set()
    for row in rows:
        key, period = row["series_key"], row["period"]
        level, comps = float(row["level"]), int(row["comp_count"])
        methodology = str(row.get("methodology_version") or "").strip()
        _validate(key, period, level, comps, now)
        if not methodology:
            raise IndexPublishError(f"{key} {period}: methodology_version is required")
        if (key, period) in seen:
            raise IndexPublishError(f"{key} {period} appears twice")
        seen.add((key, period))
        existing = _current(session, key, period)
        if existing is not None:
            if (round(existing.level, 6) == round(level, 6) and existing.comp_count == comps
                    and existing.methodology_version == methodology):
                skipped += 1
                continue
            raise IndexPublishError(f"{key} {period} is already published at {existing.level}; "
                              "published values are never overwritten, use index-correct")
        staged.append(IndexSnapshot(series_key=key, period=period, level=level, comp_count=comps,
                                    methodology_version=methodology, is_current=True,
                                    published_at=now))
    session.add_all(staged)
    return {"published": len(staged), "unchanged": skipped}


def correct(session: Session, key: str, period: str, level: float, reason: str, now: datetime,
            comp_count: Optional[int] = None) -> dict:
    """Correct a published value: the old row is kept (not current) and the change is logged."""
    existing = _current(session, key, period)
    if existing is None:
        raise IndexPublishError(f"{key} {period} has not been published")
    comps = existing.comp_count if comp_count is None else comp_count
    _validate(key, period, level, comps, now)
    if not reason.strip():
        raise IndexPublishError("a reason is required for a correction")
    existing.is_current = False
    session.add(IndexSnapshot(series_key=key, period=period, level=level, comp_count=comps,
                              methodology_version=existing.methodology_version, is_current=True,
                              published_at=now))
    session.add(IndexCorrection(series_key=key, period=period, old_level=existing.level,
                                new_level=level, reason=reason.strip(), published_at=now))
    return {"series_key": key, "period": period, "old_level": existing.level, "new_level": level}


def read_publish_file(path: str) -> list[dict]:
    """CSV with columns series_key, period, level, comp_count, methodology_version."""
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"series_key", "period", "level", "comp_count", "methodology_version"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise IndexPublishError(f"missing columns: {', '.join(sorted(missing))}")
        rows = []
        for n, row in enumerate(reader, start=2):
            try:
                rows.append({"series_key": row["series_key"].strip(), "period": row["period"].strip(),
                             "level": float(row["level"]), "comp_count": int(row["comp_count"]),
                             "methodology_version": row["methodology_version"]})
            except ValueError as exc:
                raise IndexPublishError(f"line {n}: {exc}") from exc
        return rows
