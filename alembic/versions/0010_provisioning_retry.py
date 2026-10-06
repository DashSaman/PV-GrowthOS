"""durable exclusive-provisioning retries and quota locks

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exclusive_claims",
        sa.Column("provision_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "exclusive_claims",
        sa.Column("last_provision_error", sa.Text(), nullable=True),
    )
    op.create_table(
        "provisioning_quota_locks",
        sa.Column("quota_key", sa.String(128), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provisioning_quota_locks")
    with op.batch_alter_table("exclusive_claims") as batch_op:
        batch_op.drop_column("last_provision_error")
        batch_op.drop_column("provision_attempts")
