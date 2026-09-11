"""Booking model: rename payments_meta→payment_data, booking_type values, add payment_type.

- Rename column payments_meta → payment_data
- Migrate booking_type values: branch→branch_service, home→home_service
- Drop old ck_bookings_booking_type constraint and recreate with new values
- Add payment_type column (VARCHAR 20, NOT NULL DEFAULT 'service')
- Add ck_bookings_payment_type check constraint
- Add ix_bookings_payment_type index

Revision ID: 0026_booking_payment_data_and_payment_type
Revises: 0025_payments_model_overhaul
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026_booking_payment_fields"
down_revision: str | None = "0025_payments_overhaul"
branch_labels = None
depends_on = None

_BOOKING_TYPE_CONSTRAINT = "ck_bookings_booking_type"
_PAYMENT_TYPE_CONSTRAINT = "ck_bookings_payment_type"
_PAYMENT_TYPE_INDEX = "ix_bookings_payment_type"


def _get_columns(conn, table: str) -> list[str]:
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    )
    return [row[0] for row in result]


def upgrade() -> None:
    conn = op.get_bind()
    cols = _get_columns(conn, "bookings")

    # 1. Rename payments_meta → payment_data
    if "payments_meta" in cols and "payment_data" not in cols:
        op.alter_column("bookings", "payments_meta", new_column_name="payment_data")

    # 2. Migrate booking_type values
    conn.execute(sa.text(
        "UPDATE bookings SET booking_type = 'branch_service' WHERE booking_type = 'branch'"
    ))
    conn.execute(sa.text(
        "UPDATE bookings SET booking_type = 'home_service' WHERE booking_type = 'home'"
    ))

    # 3. Drop old booking_type constraint and recreate with new values
    conn.execute(sa.text(
        "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_type"
    ))
    conn.execute(sa.text(
        "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_booking_type"
    ))
    op.create_check_constraint(
        _BOOKING_TYPE_CONSTRAINT,
        "bookings",
        "booking_type IN ('branch_service', 'home_service', 'loyalty', 'gift_voucher')",
    )

    # 4. Add payment_type column
    cols = _get_columns(conn, "bookings")  # re-fetch after rename
    if "payment_type" not in cols:
        op.add_column(
            "bookings",
            sa.Column(
                "payment_type",
                sa.String(20),
                nullable=False,
                server_default="service",
            ),
        )
        op.alter_column("bookings", "payment_type", server_default=None)

    # 5. Add payment_type check constraint
    conn.execute(sa.text(
        "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_payment_type"
    ))
    op.create_check_constraint(
        _PAYMENT_TYPE_CONSTRAINT,
        "bookings",
        "payment_type IN ('service', 'gift_voucher', 'rewarded')",
    )

    # 6. Add payment_type index
    existing = conn.execute(sa.text(
        "SELECT indexname FROM pg_indexes WHERE tablename='bookings' AND indexname=:n"
    ), {"n": _PAYMENT_TYPE_INDEX}).fetchone()
    if not existing:
        op.create_index(_PAYMENT_TYPE_INDEX, "bookings", ["payment_type"])


def downgrade() -> None:
    conn = op.get_bind()

    # Drop payment_type index, constraint, column
    conn.execute(sa.text(f"DROP INDEX IF EXISTS {_PAYMENT_TYPE_INDEX}"))
    conn.execute(sa.text(
        "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_payment_type"
    ))
    cols = _get_columns(conn, "bookings")
    if "payment_type" in cols:
        op.drop_column("bookings", "payment_type")

    # Restore old booking_type values
    conn.execute(sa.text(
        "UPDATE bookings SET booking_type='branch' WHERE booking_type='branch_service'"
    ))
    conn.execute(sa.text(
        "UPDATE bookings SET booking_type='home' WHERE booking_type='home_service'"
    ))

    # Restore old booking_type constraint
    conn.execute(sa.text(
        "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_type"
    ))
    op.create_check_constraint(
        _BOOKING_TYPE_CONSTRAINT,
        "bookings",
        "booking_type IN ('home', 'branch', 'loyalty', 'gift_voucher')",
    )

    # Rename payment_data back to payments_meta
    cols = _get_columns(conn, "bookings")
    if "payment_data" in cols and "payments_meta" not in cols:
        op.alter_column("bookings", "payment_data", new_column_name="payments_meta")
