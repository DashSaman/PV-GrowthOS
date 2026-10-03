"""phase2: free-config engine tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "config_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.String(255), nullable=True),
        sa.Column("fetch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_quality", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "raw_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("config_sources.id"), index=True),
        sa.Column("uri_hash", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("raw_uri", sa.Text(), nullable=False),
        sa.Column("remark", sa.String(255), nullable=True),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("invalid_reason", sa.String(128), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("health_checked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("health_ok", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="fetched"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "published_posts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dedupe_key", sa.String(128), unique=True, nullable=False, index=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="free_public"),
        sa.Column("config_id", sa.Integer(), sa.ForeignKey("raw_configs.id"), nullable=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "exclusive_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("claim_key", sa.String(128), unique=True, nullable=False, index=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), index=True),
        sa.Column("claim_date", sa.Date(), nullable=False),
        sa.Column("traffic_gb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("validity_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("location", sa.String(64), nullable=True),
        sa.Column("service_ref", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_provision"),
        sa.Column("config_payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("exclusive_claims")
    op.drop_table("published_posts")
    op.drop_table("raw_configs")
    op.drop_table("config_sources")
