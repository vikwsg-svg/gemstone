"""
ORM models for the enterprise platform.

All tables are prefixed ``ent_`` so they can live in the same Postgres
database as the consumer/dealer backend without colliding, while staying
logically isolated. Every tenant-owned row carries ``tenant_id``; queries
must always filter on it (see ``enterprise.routes``).
"""

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from enterprise.db import Base, JSONVariant


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "ent_tenants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # live: scans use the production scan. sandbox: example results only.
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Tenant-level model version pin (a request-level pin overrides it).
    pinned_model_version: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ApiKey(Base):
    __tablename__ = "ent_api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # public key id
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="default")
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    scopes: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Scan(Base):
    """One persisted scan: inputs, identification, comps, model version and output (audit trail)."""

    __tablename__ = "ent_scans"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False
    )
    api_key_id: Mapped[str] = mapped_column(String(40), nullable=False)
    product: Mapped[str] = mapped_column(String(16), nullable=False, default="scan")
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # processing | completed
    model_version: Mapped[Optional[str]] = mapped_column(String(64))
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    # The scan's own analysis id (used to finish a processing scan).
    analysis_ref: Mapped[Optional[str]] = mapped_column(String(64))
    photo_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The request exactly as validated, and the full response body as returned.
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    batch_job_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    batch_row: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_ent_scans_tenant_created", "tenant_id", "created_at"),)


class IdempotencyRecord(Base):
    __tablename__ = "ent_idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "endpoint", "key", name="uq_ent_idempotency_scope"),
    )


class UsageDaily(Base):
    """Metering: scans per tenant, product and UTC day."""

    __tablename__ = "ent_usage_daily"

    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), primary_key=True
    )
    product: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    scans_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scans_priced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BatchJob(Base):
    __tablename__ = "ent_batch_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False, index=True
    )
    api_key_id: Mapped[str] = mapped_column(String(40), nullable=False)
    # scan | intake | verify: which product each row runs through.
    product: Mapped[str] = mapped_column(String(16), nullable=False, default="scan",
                                         server_default="scan")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    model_version: Mapped[Optional[str]] = mapped_column(String(64))
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priced_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unpriced_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_csv: Mapped[str] = mapped_column(Text, nullable=False)
    results_csv: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ── Products built on the scan (Intake, Verify) ──────────────────────────────


class ProductSettings(Base):
    """Per-account configuration for a product (Intake routing rules, Verify thresholds)."""

    __tablename__ = "ent_product_settings"

    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), primary_key=True
    )
    product: Mapped[str] = mapped_column(String(16), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ProductRecord(Base):
    """One Intake decision or Verify check: the product result on top of a scan."""

    __tablename__ = "ent_product_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False
    )
    api_key_id: Mapped[str] = mapped_column(String(40), nullable=False)
    product: Mapped[str] = mapped_column(String(16), nullable=False)
    scan_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # processing | completed
    reference: Mapped[Optional[str]] = mapped_column(String(128))
    # Verify: the seller or claimant the subject belongs to (for repeat patterns).
    subject_key: Mapped[Optional[str]] = mapped_column(String(128))
    outcome: Mapped[Optional[str]] = mapped_column(String(32))  # decision, action or status
    score: Mapped[Optional[int]] = mapped_column(Integer)  # Verify risk score
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # The person's final call after reviewing (Intake: list|review|lab|decline;
    # Verify: confirmed|cleared), with every earlier call kept in ``review``.
    review_outcome: Mapped[Optional[str]] = mapped_column(String(32))
    review: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONVariant)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_ent_product_records_tenant_product_created", "tenant_id", "product", "created_at"),
        Index("ix_ent_product_records_tenant_outcome", "tenant_id", "product", "outcome"),
        Index("ix_ent_product_records_tenant_subject", "tenant_id", "product", "subject_key"),
    )


class PhotoFingerprint(Base):
    """A photo seen in an account, by sha256, for duplicate-imagery checks within that account."""

    __tablename__ = "ent_photo_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    product: Mapped[str] = mapped_column(String(16), nullable=False)
    record_id: Mapped[str] = mapped_column(String(40), nullable=False)
    reference: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (Index("ix_ent_photo_fingerprints_tenant_sha", "tenant_id", "sha256"),)


# ── Webhooks ─────────────────────────────────────────────────────────────────


class WebhookEndpoint(Base):
    """One webhook URL per account, with the secret its deliveries are signed with."""

    __tablename__ = "ent_webhook_endpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False, unique=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    events: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False)
    # Needed in clear to sign deliveries (HMAC); shown to the customer once.
    secret: Mapped[str] = mapped_column(String(80), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class WebhookDelivery(Base):
    """An event queued for an account's webhook (outbox): written with the record it reports."""

    __tablename__ = "ent_webhook_deliveries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # the event id, evt_...
    tenant_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ent_tenants.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_ent_webhook_deliveries_tenant_status", "tenant_id", "status"),
        Index("ix_ent_webhook_deliveries_tenant_created", "tenant_id", "created_at"),
    )
