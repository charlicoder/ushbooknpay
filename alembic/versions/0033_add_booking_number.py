"""Add booking_number to bookings table.

Adds a unique human-readable booking reference in the format B{YY}{MM}{DD}{NNN},
e.g. B260921001 for the first booking created on 2026-09-21.

The column is nullable so existing rows are unaffected.
A unique index is created to enforce reference uniqueness at the DB level.

Revision ID: 0033_add_booking_number
Revises: 0032_merge_voucher_gifts_v2
Create Date: 2026-09-21

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033_add_booking_number"
down_revision: Union[str, None] = "0032_merge_voucher_gifts_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add booking_number column (nullable VARCHAR 20) with a unique index."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "bookings" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("bookings")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("bookings")}

    # ── 1. Add column ────────────────────────────────────────────────────────
    if "booking_number" not in existing_cols:
        op.add_column(
            "bookings",
            sa.Column(
                "booking_number",
                sa.String(20),
                nullable=True,
                server_default=None,
            ),
        )

    # ── 2. Unique index ──────────────────────────────────────────────────────
    if "uq_bookings_booking_number" not in existing_indexes:
        op.create_index(
            "uq_bookings_booking_number",
            "bookings",
            ["booking_number"],
            unique=True,
            postgresql_where=sa.text("booking_number IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop booking_number unique index and column."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "bookings" not in tables:
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("bookings")}
    existing_cols = {c["name"] for c in inspector.get_columns("bookings")}

    if "uq_bookings_booking_number" in existing_indexes:
        op.drop_index("uq_bookings_booking_number", table_name="bookings")

    if "booking_number" in existing_cols:
        op.drop_column("bookings", "booking_number")
