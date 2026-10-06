"""separate gross order value from proven partner commission

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _event_metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _valid_cents(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _backfill_proven_commissions() -> None:
    bind = op.get_bind()
    event_rows = bind.execute(
        sa.text("SELECT metadata FROM events WHERE event_type = 'PARTNER_CONVERSION'")
    ).all()
    evidence: dict[tuple[int, str], list[tuple[int, int]]] = {}
    for (raw_metadata,) in event_rows:
        metadata = _event_metadata(raw_metadata)
        partner_id = metadata.get("partner_id")
        order_ref = metadata.get("order_ref")
        amount_cents = _valid_cents(metadata.get("amount_cents"))
        commission_cents = _valid_cents(metadata.get("commission_cents"))
        if (
            isinstance(partner_id, bool)
            or not isinstance(partner_id, int)
            or not isinstance(order_ref, str)
            or not order_ref
            or amount_cents is None
            or commission_cents is None
            or commission_cents > amount_cents
        ):
            continue
        evidence.setdefault((partner_id, order_ref), []).append(
            (amount_cents, commission_cents)
        )

    entry_rows = bind.execute(
        sa.text("SELECT id, partner_id, order_ref, amount_cents FROM commission_entries")
    ).all()
    for entry_id, partner_id, order_ref, amount_cents in entry_rows:
        matches = evidence.get((partner_id, order_ref), [])
        if len(matches) != 1 or matches[0][0] != amount_cents:
            continue
        bind.execute(
            sa.text(
                "UPDATE commission_entries SET commission_cents = :commission_cents "
                "WHERE id = :entry_id AND commission_cents IS NULL"
            ),
            {"commission_cents": matches[0][1], "entry_id": entry_id},
        )


def upgrade() -> None:
    op.add_column(
        "commission_entries",
        sa.Column("commission_cents", sa.Integer(), nullable=True),
    )
    _backfill_proven_commissions()


def downgrade() -> None:
    with op.batch_alter_table("commission_entries") as batch_op:
        batch_op.drop_column("commission_cents")
