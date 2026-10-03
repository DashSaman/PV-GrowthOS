"""phase4: referrals, rewards, milestones, partners, commissions

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "referral_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("code", sa.String(32), unique=True, nullable=False, index=True),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("referrer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referred_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("qualifying_order_ref", sa.String(128), nullable=True),
        sa.Column("validated_at", sa.DateTime(), nullable=True),
        sa.Column("rewarded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("referred_user_id", name="uq_referral_referred_user"),
    )
    op.create_index("ix_referral_referrer", "referrals", ["referrer_id"])
    op.create_table(
        "reward_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(128), unique=True, nullable=False),
        sa.Column("meta", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "milestones",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), unique=True, nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("reward_kind", sa.String(16), nullable=False),
        sa.Column("reward_amount", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "partners",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("code", sa.String(32), unique=True, nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="affiliate"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meta", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "commission_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=False, index=True),
        sa.Column("order_ref", sa.String(128), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("dedupe_key", sa.String(128), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("commission_entries")
    op.drop_table("partners")
    op.drop_table("milestones")
    op.drop_table("reward_ledger")
    op.drop_index("ix_referral_referrer", table_name="referrals")
    op.drop_table("referrals")
    op.drop_table("referral_codes")
