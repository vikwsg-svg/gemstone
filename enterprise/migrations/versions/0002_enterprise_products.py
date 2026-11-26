"""Intake and Verify on the scan: records, settings, reviews, product batch, webhooks.

Revision ID: 0002_enterprise_products
Revises: 0001_enterprise_scan_v1
Create Date: 2026-11-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_enterprise_products"
down_revision = "0001_enterprise_scan_v1"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "ent_product_settings",
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), primary_key=True),
        sa.Column("product", sa.String(16), primary_key=True),
        sa.Column("config", JSON, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "ent_product_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("api_key_id", sa.String(40), nullable=False),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("scan_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("subject_key", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("request_payload", JSON, nullable=False),
        sa.Column("result", JSON, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("review_outcome", sa.String(32), nullable=True),
        sa.Column("review", JSON, nullable=True),
        sa.Column("reviewed_at", TS, nullable=True),
    )
    op.create_index("ix_ent_product_records_scan_id", "ent_product_records", ["scan_id"])
    op.create_index("ix_ent_product_records_tenant_product_created", "ent_product_records",
                    ["tenant_id", "product", "created_at"])
    op.create_index("ix_ent_product_records_tenant_subject", "ent_product_records",
                    ["tenant_id", "product", "subject_key"])
    op.create_table(
        "ent_photo_fingerprints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("record_id", sa.String(40), nullable=False),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_ent_photo_fingerprints_tenant_sha", "ent_photo_fingerprints",
                    ["tenant_id", "sha256"])
    op.create_index("ix_ent_product_records_tenant_outcome", "ent_product_records",
                    ["tenant_id", "product", "outcome"])
    op.add_column("ent_batch_jobs", sa.Column("product", sa.String(16), nullable=False,
                                              server_default="scan"))
    op.create_table(
        "ent_webhook_endpoints",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False,
                  unique=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("events", JSON, nullable=False),
        sa.Column("secret", sa.String(80), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "ent_webhook_deliveries",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("ent_tenants.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_attempt_at", TS, nullable=True),
        sa.Column("delivered_at", TS, nullable=True),
    )
    op.create_index("ix_ent_webhook_deliveries_tenant_status", "ent_webhook_deliveries",
                    ["tenant_id", "status"])
    op.create_index("ix_ent_webhook_deliveries_tenant_created", "ent_webhook_deliveries",
                    ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ent_webhook_deliveries_tenant_created", table_name="ent_webhook_deliveries")
    op.drop_index("ix_ent_webhook_deliveries_tenant_status", table_name="ent_webhook_deliveries")
    op.drop_table("ent_webhook_deliveries")
    op.drop_table("ent_webhook_endpoints")
    with op.batch_alter_table("ent_batch_jobs") as batch:
        batch.drop_column("product")
    op.drop_index("ix_ent_product_records_tenant_outcome", table_name="ent_product_records")
    op.drop_index("ix_ent_photo_fingerprints_tenant_sha", table_name="ent_photo_fingerprints")
    op.drop_table("ent_photo_fingerprints")
    op.drop_index("ix_ent_product_records_tenant_subject", table_name="ent_product_records")
    op.drop_index("ix_ent_product_records_tenant_product_created", table_name="ent_product_records")
    op.drop_index("ix_ent_product_records_scan_id", table_name="ent_product_records")
    op.drop_table("ent_product_records")
    op.drop_table("ent_product_settings")
