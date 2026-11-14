"""Home services & multi-therapist: add a 3rd therapist to ``bookings``.

Adds two nullable columns plus a sparse index:

* ``tertiary_therapist_id``   - UUID of the 3rd therapist (when up to 3 therapists are needed)
* ``tertiary_therapist_data`` - JSONB snapshot of that therapist

Revision ID: 0071_booking_tertiary_therapist
Revises: 0070_voucher_ushdesk_ref_fill
Create Date: 2026-11-12
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0071_booking_tertiary_therapist"
down_revision: Union[str, None] = "0070_voucher_ushdesk_ref_fill"
branch_labels = None
depends_on = None

_INDEX = "ix_bookings_tertiary_therapist_date"


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS tertiary_therapist_id UUID")
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS tertiary_therapist_data JSONB")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} "
        "ON bookings (tertiary_therapist_id, appointment_date) "
        "WHERE tertiary_therapist_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS tertiary_therapist_data")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS tertiary_therapist_id")

