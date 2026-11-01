"""Add bookings.sessions (multi-session bookings).

Additive and backward compatible: JSONB NOT NULL with default '[]'. Legacy rows keep an
empty list; readers synthesize one session from the legacy columns (get_booking_sessions),
so no data backfill is required.

Revision ID: 0059_booking_sessions
Revises: 0058_voucher_scheduled_delivery
Create Date: 2026-11-01
"""

from __future__ import annotations

from typing import Union

from alembic import op


revision: str = "0059_booking_sessions"
down_revision: Union[str, None] = "0058_voucher_scheduled_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE bookings "
        "ADD COLUMN IF NOT EXISTS sessions JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS sessions")
