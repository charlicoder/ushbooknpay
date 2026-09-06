"""
0006_add_booking_type_make_fields_optional
──────────────────────────────────────────
Add booking_type (VARCHAR 20, NOT NULL, DEFAULT 'branch') column to bookings table.
Make branch_id and service_arrangement_id nullable to support home-service bookings.

Revision ID: 0006_add_booking_type_make_fields_optional
Revises: 0005_booking_payments_meta
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_booking_type"
down_revision = "0005_booking_payments_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("bookings")}

    # ── 1. Add booking_type column ──────────────────────────────────────
    if "booking_type" not in cols:
        op.add_column(
            "bookings",
            sa.Column(
                "booking_type",
                sa.String(20),
                nullable=False,
                server_default="branch",
            ),
        )

    # ── 2. Add CheckConstraint for booking_type ──────────────────────────
    # Use a try/except because the constraint may already exist on re-runs.
    try:
        op.create_check_constraint(
            "ck_bookings_booking_type",
            "bookings",
            "booking_type IN ('home', 'branch')",
        )
    except Exception:
        pass

    # ── 3. Add index on booking_type ─────────────────────────────────────
    try:
        op.create_index("ix_bookings_booking_type", "bookings", ["booking_type"])
    except Exception:
        pass

    # ── 4. Make branch_id nullable ───────────────────────────────────────
    if "branch_id" in cols:
        op.alter_column("bookings", "branch_id", nullable=True)

    # ── 5. Make service_arrangement_id nullable ───────────────────────────
    if "service_arrangement_id" in cols:
        op.alter_column("bookings", "service_arrangement_id", nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("bookings")}

    # ── Reverse: restore NOT NULL on service_arrangement_id ──────────────
    if "service_arrangement_id" in cols:
        # Backfill NULLs with a sentinel UUID before restoring NOT NULL
        op.execute(
            "UPDATE bookings SET service_arrangement_id = gen_random_uuid() "
            "WHERE service_arrangement_id IS NULL"
        )
        op.alter_column("bookings", "service_arrangement_id", nullable=False)

    # ── Reverse: restore NOT NULL on branch_id ───────────────────────────
    if "branch_id" in cols:
        op.execute(
            "UPDATE bookings SET branch_id = gen_random_uuid() "
            "WHERE branch_id IS NULL"
        )
        op.alter_column("bookings", "branch_id", nullable=False)

    # ── Reverse: drop booking_type index, constraint, and column ─────────
    try:
        op.drop_index("ix_bookings_booking_type", table_name="bookings")
    except Exception:
        pass

    try:
        op.drop_constraint("ck_bookings_booking_type", "bookings", type_="check")
    except Exception:
        pass

    if "booking_type" in cols:
        op.drop_column("bookings", "booking_type")
