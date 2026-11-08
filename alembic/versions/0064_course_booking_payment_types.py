"""Course redemption bookings and course purchase payments.

* ``bookings.course_subscription_id`` (UUID, nullable, indexed) + ``bookings.course_data`` (JSONB)
* ``payments.course_subscription_id`` (UUID, nullable, indexed)
* ``ck_bookings_booking_type`` / ``ck_bookings_payment_type`` recreated as SUPERSETS
  (adds ``course_redemption`` / ``course_subscription``) — existing rows stay valid.
* ``payment_links.target_type`` / ``payments.payment_for`` are free VARCHARs without a DB
  check, so the new ``course_subscription`` value needs no DDL.

The downgrade restores the previous checks; it fails if course rows still exist
(delete / migrate them first), which is intentional.

Revision ID: 0064_course_booking_payment
Revises: 0063_course_subscriptions
Create Date: 2026-11-06
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0064_course_booking_payment"
down_revision: Union[str, None] = "0063_course_subscriptions"
branch_labels = None
depends_on = None

_BOOKING_TYPES_OLD = "('branch_service', 'home_service', 'loyalty', 'gift_voucher')"
_BOOKING_TYPES_NEW = "('branch_service', 'home_service', 'loyalty', 'gift_voucher', 'course_redemption')"
_PAYMENT_TYPES_OLD = "('service', 'gift_voucher', 'rewarded')"
_PAYMENT_TYPES_NEW = "('service', 'gift_voucher', 'rewarded', 'course_subscription')"


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS course_subscription_id UUID")
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS course_data JSONB")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bookings_course_subscription_id ON bookings (course_subscription_id)"
    )
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS course_subscription_id UUID")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payments_course_subscription_id ON payments (course_subscription_id)"
    )

    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_type")
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_booking_type")
    op.execute(
        f"ALTER TABLE bookings ADD CONSTRAINT ck_bookings_booking_type CHECK (booking_type IN {_BOOKING_TYPES_NEW})"
    )
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_payment_type")
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_payment_type")
    op.execute(
        f"ALTER TABLE bookings ADD CONSTRAINT ck_bookings_payment_type CHECK (payment_type IN {_PAYMENT_TYPES_NEW})"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_payment_type")
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_payment_type")
    op.execute(
        f"ALTER TABLE bookings ADD CONSTRAINT ck_bookings_payment_type CHECK (payment_type IN {_PAYMENT_TYPES_OLD})"
    )
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_type")
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_booking_type")
    op.execute(
        f"ALTER TABLE bookings ADD CONSTRAINT ck_bookings_booking_type CHECK (booking_type IN {_BOOKING_TYPES_OLD})"
    )
    op.execute("DROP INDEX IF EXISTS ix_payments_course_subscription_id")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS course_subscription_id")
    op.execute("DROP INDEX IF EXISTS ix_bookings_course_subscription_id")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS course_data")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS course_subscription_id")

