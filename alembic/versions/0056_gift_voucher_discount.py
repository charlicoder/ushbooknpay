"""Add discount column to gift_vouchers table.

Revision ID: 0056_gift_voucher_discount
Revises: 0055_booking_secondary_therapist
Create Date: 2026-10-28
"""

from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0056_gift_voucher_discount"
down_revision: Union[str, None] = "0055_booking_secondary_therapist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gift_vouchers "
        "ADD COLUMN IF NOT EXISTS discount NUMERIC(10, 3) NOT NULL DEFAULT 0.000"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS discount")
