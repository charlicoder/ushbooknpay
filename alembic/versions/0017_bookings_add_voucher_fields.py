"""Add voucher_id and voucher_data columns to bookings table.

Revision ID: 0017_bookings_add_voucher_fields
Revises: 0016_gift_vouchers_table
Create Date: 2026-09-04 00:00:00.000000

"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017_bookings_add_voucher_fields"
down_revision: Union[str, None] = "0016_gift_vouchers_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("bookings")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("bookings")}

    # ── 1. Add voucher_id (nullable UUID, no FK) ─────────────────────────────
    if "voucher_id" not in existing_cols:
        op.add_column(
            "bookings",
            sa.Column(
                "voucher_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # ── 2. Add voucher_data (nullable JSONB snapshot) ────────────────────────
    if "voucher_data" not in existing_cols:
        op.add_column(
            "bookings",
            sa.Column(
                "voucher_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )

    # ── 3. Index on voucher_id for fast lookups ──────────────────────────────
    if "ix_bookings_voucher_id" not in existing_indexes:
        op.create_index("ix_bookings_voucher_id", "bookings", ["voucher_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" not in tables:
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("bookings")}
    existing_cols = {c["name"] for c in inspector.get_columns("bookings")}

    if "ix_bookings_voucher_id" in existing_indexes:
        op.drop_index("ix_bookings_voucher_id", table_name="bookings")

    if "voucher_data" in existing_cols:
        op.drop_column("bookings", "voucher_data")

    if "voucher_id" in existing_cols:
        op.drop_column("bookings", "voucher_id")
