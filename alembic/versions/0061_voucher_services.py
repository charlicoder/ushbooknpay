"""Add multi-service support to gift_vouchers.

Revision ID: 0061_voucher_services
Revises: 0060_appointment_cache_outbox
Create Date: 2026-11-04
"""

from __future__ import annotations

from typing import Union

from alembic import op


revision: str = "0061_voucher_services"
down_revision: Union[str, None] = "0060_appointment_cache_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy single-service vouchers keep NULL and are read as a one-entry list.
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS services JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS services")
