"""Add voucher_number to gift_vouchers table.

Adds a unique human-readable voucher reference in the format V{YY}{MM}{DD}{NNN},
e.g. V260921001 for the first voucher created on 2026-09-21.

The column is nullable so existing rows are unaffected.
A partial unique index (WHERE voucher_number IS NOT NULL) allows multiple NULL
rows (legacy records) while enforcing uniqueness for all new non-null values.

Revision ID: 0034_add_voucher_number
Revises: 0033_add_booking_number
Create Date: 2026-09-22

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034_add_voucher_number"
down_revision: Union[str, None] = "0033_add_booking_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add voucher_number column (nullable VARCHAR 20) with a partial unique index."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}

    # ── 1. Add column ─────────────────────────────────────────────────────────
    if "voucher_number" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "voucher_number",
                sa.String(20),
                nullable=True,
                server_default=None,
            ),
        )

    # ── 2. Partial unique index (NULLs not compared, so legacy rows are safe) ─
    if "uq_gift_vouchers_voucher_number" not in existing_indexes:
        op.create_index(
            "uq_gift_vouchers_voucher_number",
            "gift_vouchers",
            ["voucher_number"],
            unique=True,
            postgresql_where=sa.text("voucher_number IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop voucher_number unique index and column."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "gift_vouchers" not in tables:
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}
    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    if "uq_gift_vouchers_voucher_number" in existing_indexes:
        op.drop_index("uq_gift_vouchers_voucher_number", table_name="gift_vouchers")

    if "voucher_number" in existing_cols:
        op.drop_column("gift_vouchers", "voucher_number")
