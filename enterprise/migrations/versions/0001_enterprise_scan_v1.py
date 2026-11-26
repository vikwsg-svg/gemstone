"""Enterprise Scan API v1: tenants, scoped API keys, scans, idempotency, usage, batch jobs.

Revision ID: 0001_enterprise_scan_v1
Revises:
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_enterprise_scan_v1"
down_revision = None
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ent_tenants",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pinned_model_version", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "ent_api_keys",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
    )
    op.create_index("ix_ent_api_keys_tenant_id", "ent_api_keys", ["tenant_id"])

    op.create_table(
        "ent_scans",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("api_key_id", sa.String(40), nullable=False),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("analysis_ref", sa.String(64), nullable=True),
        sa.Column("photo_count", sa.Integer(), nullable=False),
        sa.Column("priced", sa.Boolean(), nullable=False),
        sa.Column("request_payload", JSON, nullable=False),
        sa.Column("result", JSON, nullable=False),
        sa.Column("batch_job_id", sa.String(40), nullable=True),
        sa.Column("batch_row", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
    )
    op.create_index("ix_ent_scans_batch_job_id", "ent_scans", ["batch_job_id"])
    op.create_index("ix_ent_scans_tenant_created", "ent_scans", ["tenant_id", "created_at"])

    op.create_table(
        "ent_idempotency_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("endpoint", sa.String(64), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("tenant_id", "endpoint", "key", name="uq_ent_idempotency_scope"),
    )

    op.create_table(
        "ent_usage_daily",
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), primary_key=True),
        sa.Column("product", sa.String(16), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("scans_total", sa.Integer(), nullable=False),
        sa.Column("scans_priced", sa.Integer(), nullable=False),
    )

    op.create_table(
        "ent_batch_jobs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("api_key_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("processed_rows", sa.Integer(), nullable=False),
        sa.Column("priced_rows", sa.Integer(), nullable=False),
        sa.Column("unpriced_rows", sa.Integer(), nullable=False),
        sa.Column("failed_rows", sa.Integer(), nullable=False),
        sa.Column("input_csv", sa.Text(), nullable=False),
        sa.Column("results_csv", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("completed_at", TS, nullable=True),
    )
    op.create_index("ix_ent_batch_jobs_tenant_id", "ent_batch_jobs", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_ent_batch_jobs_tenant_id", table_name="ent_batch_jobs")
    op.drop_table("ent_batch_jobs")
    op.drop_table("ent_usage_daily")
    op.drop_table("ent_idempotency_keys")
    op.drop_index("ix_ent_scans_tenant_created", table_name="ent_scans")
    op.drop_index("ix_ent_scans_batch_job_id", table_name="ent_scans")
    op.drop_table("ent_scans")
    op.drop_index("ix_ent_api_keys_tenant_id", table_name="ent_api_keys")
    op.drop_table("ent_api_keys")
    op.drop_table("ent_tenants")
