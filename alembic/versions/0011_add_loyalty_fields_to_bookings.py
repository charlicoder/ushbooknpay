"""Add loyalty_data, reward_id to bookings; extend booking_type constraint.

Revision ID: 0011_add_loyalty_fields_bookings
Revises: 0010_add_service_name_loyalty
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0011_add_loyalty_fields_bookings"
down_revision: Union[str, None] = "0010_add_service_name_loyalty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add loyalty_data JSONB column (nullable)
    op.add_column(
        "bookings",
        sa.Column("loyalty_data", JSONB, nullable=True),
    )

    # 2. Add reward_id UUID column (nullable)
    op.add_column(
        "bookings",
        sa.Column("reward_id", UUID(as_uuid=True), nullable=True),
    )

    # 3. Drop old booking_type check constraint (only 'home', 'branch')
    op.drop_constraint("ck_bookings_booking_type", "bookings", type_="check")

    # 4. Recreate to also allow 'loyalty'
    op.create_check_constraint(
        "ck_bookings_booking_type",
        "bookings",
        "booking_type IN ('home', 'branch', 'loyalty')",
    )

    # 5. Index on reward_id for fast loyalty booking lookup
    op.create_index("ix_bookings_reward_id", "bookings", ["reward_id"])


def downgrade() -> None:
    op.drop_index("ix_bookings_reward_id", table_name="bookings")

    op.drop_constraint("ck_bookings_booking_type", "bookings", type_="check")
    op.create_check_constraint(
        "ck_bookings_booking_type",
        "bookings",
        "booking_type IN ('home', 'branch')",
    )

    op.drop_column("bookings", "reward_id")
    op.drop_column("bookings", "loyalty_data")
