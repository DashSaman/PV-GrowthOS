"""phase1: users, campaigns, sources, events, attribution

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), unique=True, nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("language_code", sa.String(16), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mirza_user_ref", sa.String(128), nullable=True, index=True),
        sa.Column("lead_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segment", sa.String(32), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("config", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
              nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(128), unique=True, nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="organic"),
        sa.Column("medium", sa.String(64), nullable=True),
        sa.Column("referrer", sa.String(255), nullable=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True,
              index=True),
        sa.Column("bot_starts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("purchases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("renewal_revenue_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("event_type", sa.String(32), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True,
              index=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=True,
              index=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True, nullable=False),
        sa.Column("metadata", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(),
              "postgresql"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_table(
        "attribution_touch",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("raw_start_param", sa.String(255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.UniqueConstraint("user_id", "kind", name="uq_touch_user_kind"),
    )


def downgrade() -> None:
    op.drop_table("attribution_touch")
    op.drop_table("events")
    op.drop_table("sources")
    op.drop_table("campaigns")
    op.drop_table("users")
