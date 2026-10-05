"""instagram growth: creative metadata, publications, and insights

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "content_items",
        sa.Column("format", sa.String(16), nullable=False, server_default="text"),
    )
    op.add_column(
        "content_items",
        sa.Column("creative", JSONB, nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_table(
        "content_publications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("content_id", sa.Integer(), sa.ForeignKey("content_items.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("container_id", sa.String(128), nullable=True),
        sa.Column("media_id", sa.String(128), nullable=True),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("provider", "content_id", name="uq_content_publication_provider_content"),
        sa.UniqueConstraint("provider", "container_id", name="uq_content_publication_provider_container"),
        sa.UniqueConstraint("provider", "media_id", name="uq_content_publication_provider_media"),
    )
    op.create_index("ix_content_publications_content_id", "content_publications", ["content_id"])
    op.create_index("ix_content_publications_status", "content_publications", ["status"])
    op.create_table(
        "content_insights",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "publication_id",
            sa.Integer(),
            sa.ForeignKey("content_publications.id"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("metrics", JSONB, nullable=False),
        sa.UniqueConstraint("publication_id", "captured_at", name="uq_content_insight_snapshot"),
    )
    op.create_index("ix_content_insights_publication_id", "content_insights", ["publication_id"])


def downgrade() -> None:
    op.drop_index("ix_content_insights_publication_id", table_name="content_insights")
    op.drop_table("content_insights")
    op.drop_index("ix_content_publications_status", table_name="content_publications")
    op.drop_index("ix_content_publications_content_id", table_name="content_publications")
    op.drop_table("content_publications")
    with op.batch_alter_table("content_items") as batch_op:
        batch_op.drop_column("creative")
        batch_op.drop_column("format")
