"""Add update_history column to gift_vouchers table.

Revision ID: 0057_voucher_update_history
Revises: 0056_gift_voucher_discount
Create Date: 2026-10-28
"""

from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0057_voucher_update_history"
down_revision: Union[str, None] = "0056_gift_voucher_discount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gift_vouchers "
        "ADD COLUMN IF NOT EXISTS update_history JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS update_history")

