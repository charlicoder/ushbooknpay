"""
0007_promotions_loyalty_tables
───────────────────────────────
Creates the promotions loyalty tables:
  - promotions_loyalty_tracker
  - promotions_loyalty_reward

Revision ID: 0007_promotions_loyalty
Revises: 0006_booking_type
Create Date: 2026-08-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0007_promotions_loyalty"
down_revision = "0006_booking_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ── promotions_loyalty_tracker ────────────────────────────────────────────
    if "promotions_loyalty_tracker" not in existing_tables:
        op.create_table(
            "promotions_loyalty_tracker",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_arrangement_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("booking_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("bookings_required", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("total_bookings", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_rewards_earned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            # Unique constraint: one tracker per (customer, service, arrangement)
            sa.UniqueConstraint(
                "customer_id",
                "service_id",
                "service_arrangement_id",
                name="uq_promotions_tracker_customer_service_arrangement",
            ),
        )
        op.create_index(
            "ix_promotions_tracker_customer_id",
            "promotions_loyalty_tracker",
            ["customer_id"],
        )
        op.create_index(
            "ix_promotions_tracker_service_id",
            "promotions_loyalty_tracker",
            ["service_id"],
        )
        op.create_index(
            "ix_promotions_tracker_lookup",
            "promotions_loyalty_tracker",
            ["customer_id", "service_id", "service_arrangement_id"],
        )

    # ── promotions_loyalty_reward ─────────────────────────────────────────────
    if "promotions_loyalty_reward" not in existing_tables:
        op.create_table(
            "promotions_loyalty_reward",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_arrangement_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "tracker_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("promotions_loyalty_tracker.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="available",
            ),
            sa.Column(
                "earned_from_booking_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("bookings.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "redeemed_in_booking_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("bookings.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_promotions_reward_customer_status",
            "promotions_loyalty_reward",
            ["customer_id", "status"],
        )
        op.create_index(
            "ix_promotions_reward_service_status",
            "promotions_loyalty_reward",
            ["service_id", "status"],
        )
        op.create_index(
            "ix_promotions_reward_expires_at",
            "promotions_loyalty_reward",
            ["expires_at"],
        )


def downgrade() -> None:
    op.drop_table("promotions_loyalty_reward")
    op.drop_table("promotions_loyalty_tracker")
