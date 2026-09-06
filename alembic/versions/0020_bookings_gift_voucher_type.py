"""Add gift_voucher to bookings.booking_type check constraint.

The ck_bookings_booking_type constraint previously only allowed
('home', 'branch', 'loyalty').  Gift-voucher bookings need the
value 'gift_voucher', which this migration adds.

NOTE: We use raw op.execute() SQL rather than op.create_check_constraint()
because Alembic's helper auto-prepends the table name to the constraint
name, which would produce "ck_bookings_ck_bookings_booking_type" instead
of "ck_bookings_booking_type".

Revision ID: 0020_bookings_gift_voucher_type
Revises: 0019_voucher_payment_url
Create Date: 2026-09-04 00:00:00.000000
"""

from __future__ import annotations

from typing import Union

from alembic import op

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0020_bookings_gift_voucher_type"
down_revision: Union[str, None] = "0019_voucher_payment_url"
branch_labels = None
depends_on = None

# Exact SQL constraint names — never go through Alembic helpers to avoid
# the automatic table-name prefix that doubles the name.
_CONSTRAINT_NAME = "ck_bookings_booking_type"
# Alembic helpers incorrectly produced this doubled name on the first run;
# we drop it too so the migration is safe to re-run.
_DOUBLED_CONSTRAINT_NAME = "ck_bookings_ck_bookings_booking_type"

_OLD_CHECK = "booking_type IN ('home', 'branch', 'loyalty')"
_NEW_CHECK = "booking_type IN ('home', 'branch', 'loyalty', 'gift_voucher')"


def upgrade() -> None:
    # Drop both possible names so this is idempotent regardless of prior state
    op.execute(
        f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {_DOUBLED_CONSTRAINT_NAME}"
    )
    op.execute(
        f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"
    )
    # Add the constraint with the correct name and the new allowed values
    op.execute(
        f"ALTER TABLE bookings ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"CHECK ({_NEW_CHECK})"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {_DOUBLED_CONSTRAINT_NAME}"
    )
    op.execute(
        f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"
    )
    op.execute(
        f"ALTER TABLE bookings ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"CHECK ({_OLD_CHECK})"
    )
