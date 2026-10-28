"""Couple/double bookings: add a second therapist to ``bookings``.

Adds two nullable columns (metadata-only, no table rewrite) plus a sparse index:

* ``secondary_therapist_id``   - UUID of the 2nd therapist (couple_room / double_room)
* ``secondary_therapist_data`` - JSONB snapshot of that therapist

``therapist_id`` / ``therapist_data`` keep describing the primary therapist, so
all existing clients and queries behave exactly as before.

Revision ID: 0055_booking_secondary_therapist
Revises: 0054_payment_provider_trace
Create Date: 2026-10-27
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0055_booking_secondary_therapist"
down_revision: Union[str, None] = "0054_payment_provider_trace"
branch_labels = None
depends_on = None

_INDEX = "ix_bookings_secondary_therapist_date"


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS secondary_therapist_id UUID")
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS secondary_therapist_data JSONB")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} "
        "ON bookings (secondary_therapist_id, appointment_date) "
        "WHERE secondary_therapist_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS secondary_therapist_data")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS secondary_therapist_id")
