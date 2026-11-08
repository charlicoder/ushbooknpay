"""Add created_by_name, created_by_role, created_by_user_data to gift_vouchers.

Revision ID: 0065_voucher_created_by_user
Revises: 0064_course_booking_payment
Create Date: 2026-11-07
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0065_voucher_created_by_user"
down_revision: Union[str, None] = "0064_course_booking_payment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS created_by_name VARCHAR(255);")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS created_by_role VARCHAR(100);")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS created_by_user_data JSONB DEFAULT '{}'::jsonb;")


def downgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS created_by_name;")
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS created_by_role;")
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS created_by_user_data;")

