"""Add partial-redemption history to gift_vouchers.

Revision ID: 0062_voucher_redemptions
Revises: 0061_voucher_services
Create Date: 2026-11-04
"""

from __future__ import annotations

from typing import Union

from alembic import op


revision: str = "0062_voucher_redemptions"
down_revision: Union[str, None] = "0061_voucher_services"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS redemptions JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS redemptions")
