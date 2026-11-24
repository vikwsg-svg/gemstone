"""Usage metering: count of valuations per tenant, product and UTC day."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from enterprise.models import UsageDaily


def record_valuation(
    session: Session, *, tenant_id: str, product: str, day: date, priced: bool
) -> None:
    """Increment the tenant's counter for ``day`` in the caller's transaction (atomic upsert)."""
    dialect = session.get_bind().dialect.name
    priced_inc = 1 if priced else 0
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        stmt = insert(UsageDaily).values(
            tenant_id=tenant_id,
            product=product,
            day=day,
            valuations_total=1,
            valuations_priced=priced_inc,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UsageDaily.tenant_id, UsageDaily.product, UsageDaily.day],
            set_={
                "valuations_total": UsageDaily.valuations_total + 1,
                "valuations_priced": UsageDaily.valuations_priced + priced_inc,
            },
        )
        session.execute(stmt)
        return

    # Portable fallback for other databases.
    row = session.get(UsageDaily, (tenant_id, product, day))
    if row is None:
        session.add(
            UsageDaily(
                tenant_id=tenant_id,
                product=product,
                day=day,
                valuations_total=1,
                valuations_priced=priced_inc,
            )
        )
    else:
        row.valuations_total += 1
        row.valuations_priced += priced_inc


def usage_for(session: Session, tenant_id: str, start: date, end: date) -> list[UsageDaily]:
    return list(
        session.scalars(
            select(UsageDaily)
            .where(
                UsageDaily.tenant_id == tenant_id,
                UsageDaily.day >= start,
                UsageDaily.day <= end,
            )
            .order_by(UsageDaily.day, UsageDaily.product)
        )
    )
