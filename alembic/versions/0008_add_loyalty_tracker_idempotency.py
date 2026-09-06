"""
0008_add_loyalty_tracker_idempotency
──────────────────────────────────────
Adds ``last_recorded_booking_id`` to ``promotions_loyalty_tracker``.

This nullable UUID column acts as an idempotency guard: when a
``booking.confirmed`` SQS event is delivered more than once (at-least-once
delivery), the second call is detected and skipped without double-incrementing
``booking_count``.

Revision ID: 0008_loyalty_idempotency
Revises: 0007_promotions_loyalty
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0008_loyalty_idempotency"
down_revision = "0007_promotions_loyalty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Guard: only add the column if it doesn't already exist
    existing_cols = {
        col["name"]
        for col in inspector.get_columns("promotions_loyalty_tracker")
    }
    if "last_recorded_booking_id" not in existing_cols:
        op.add_column(
            "promotions_loyalty_tracker",
            sa.Column(
                "last_recorded_booking_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("promotions_loyalty_tracker", "last_recorded_booking_id")
