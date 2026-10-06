"""Model lifecycle delivery attempts and stable send ordinals.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("message_log") as batch:
        batch.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("send_ordinal", sa.Integer(), nullable=False, server_default="1")
        )
        batch.alter_column(
            "status",
            existing_type=sa.String(length=16),
            server_default="reserved",
        )
        batch.alter_column(
            "sent_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )
    op.create_index(
        "ix_message_log_delivery_slot",
        "message_log",
        ["purpose", "user_id", "send_ordinal", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_log_delivery_slot", table_name="message_log")
    op.execute(
        sa.text(
            "UPDATE message_log SET sent_at = CURRENT_TIMESTAMP "
            "WHERE sent_at IS NULL"
        )
    )
    with op.batch_alter_table("message_log") as batch:
        batch.alter_column(
            "sent_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
        batch.alter_column(
            "status",
            existing_type=sa.String(length=16),
            server_default="sent",
        )
        batch.drop_column("send_ordinal")
        batch.drop_column("attempts")
