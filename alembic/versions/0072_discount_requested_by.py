"""Add discount_requested_by column to bookings and gift_vouchers tables.

Adds a nullable ``discount_requested_by`` (VARCHAR(255)) column to both
``bookings`` and ``gift_vouchers`` to record who requested a manual discount.

Revision ID: 0072_discount_requested_by
Revises: 0071_booking_tertiary_therapist
Create Date: 2026-11-26
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0072_discount_requested_by"
down_revision: Union[str, None] = "0071_booking_tertiary_therapist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE bookings "
        "ADD COLUMN IF NOT EXISTS discount_requested_by VARCHAR(255)"
    )
    op.execute(
        "ALTER TABLE gift_vouchers "
        "ADD COLUMN IF NOT EXISTS discount_requested_by VARCHAR(255)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS discount_requested_by")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS discount_requested_by")
