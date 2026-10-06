"""Exact free quotas and durable lottery/shared-public reservations."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("exclusive_claims") as batch:
        batch.add_column(sa.Column("traffic_bytes", sa.BigInteger(), nullable=True))
    op.execute(sa.text("UPDATE exclusive_claims SET traffic_bytes = CAST(traffic_gb AS BIGINT) * 1073741824 WHERE traffic_gb > 0"))
    op.create_table(
        "free_allocations",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("bucket", sa.String(16), nullable=False),
        sa.Column("traffic_bytes", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id")),
        sa.Column("claim_id", sa.Integer(), sa.ForeignKey("exclusive_claims.id")),
        sa.Column("service_ref", sa.String(128), unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.CheckConstraint("traffic_bytes > 0", name="ck_free_allocation_positive"),
    )
    op.create_index("ix_free_allocations_day", "free_allocations", ["day"])
    op.create_index("ix_free_allocations_bucket", "free_allocations", ["bucket"])
    op.create_table(
        "lottery_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("draw_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("allocation_id", sa.String(128), sa.ForeignKey("free_allocations.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("campaign_id", "user_id", "draw_date", name="uq_lottery_entry_day"),
    )
    op.create_table(
        "lottery_draws",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("draw_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("campaign_id", "draw_date", name="uq_lottery_draw_day"),
    )
    for table in ("lottery_entries", "lottery_draws"):
        for field in ("campaign_id", "draw_date"):
            op.create_index(f"ix_{table}_{field}", table, [field])
    op.create_index("ix_lottery_entries_user_id", "lottery_entries", ["user_id"])


def downgrade():
    op.drop_table("lottery_draws")
    op.drop_table("lottery_entries")
    op.drop_table("free_allocations")
    with op.batch_alter_table("exclusive_claims") as batch:
        batch.drop_column("traffic_bytes")
