"""phase3: durable jobs, templates, message log, lifecycle rules

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_type", sa.String(64), nullable=False, index=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True, nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("retry_after", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_jobs_due", "jobs", ["status", "scheduled_at"])

    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("intent", sa.String(32), nullable=False, index=True),
        sa.Column("locale", sa.String(8), nullable=False, server_default="fa"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("cta_label", sa.String(64), nullable=True),
        sa.Column("cta_url", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "message_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False, index=True),
        sa.Column("dedupe_key", sa.String(255), unique=True, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="sent"),
        sa.Column("meta", JSONB, nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "lifecycle_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("trigger", sa.String(64), nullable=False),
        sa.Column("delay_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conditions", JSONB, nullable=False),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column("cta_kind", sa.String(32), nullable=True),
        sa.Column("cooldown_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("max_sends", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stop_conditions", JSONB, nullable=False),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lifecycle_rules")
    op.drop_table("message_log")
    op.drop_table("message_templates")
    op.drop_index("ix_jobs_due", table_name="jobs")
    op.drop_table("jobs")
