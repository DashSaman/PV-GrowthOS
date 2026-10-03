"""phase5: content items, feedback, competitor watch

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "content_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="education"),
        sa.Column("channel", sa.String(32), nullable=False, server_default="free"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("facts", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft", index=True),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column("campaign_code", sa.String(64), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_content_dedupe"),
    )
    op.create_table(
        "feedback_ratings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("testimonial_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dedupe_key", sa.String(128), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "competitor_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="http_text"),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_snapshot", JSONB, nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "competitor_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("competitor_sources.id"),
              nullable=False, index=True),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("old_value", sa.String(255), nullable=True),
        sa.Column("new_value", sa.String(255), nullable=True),
        sa.Column("dedupe_key", sa.String(255), unique=True, nullable=False),
        sa.Column("source_url", sa.String(512), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("competitor_changes")
    op.drop_table("competitor_sources")
    op.drop_table("feedback_ratings")
    op.drop_table("content_items")
