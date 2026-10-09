"""Add voucher_number column to payments table.

Revision ID: 0051_add_payment_voucher_number
Revises: 0050_add_refunds_table
Create Date: 2026-10-08
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051_add_payment_voucher_number"
down_revision: Union[str, None] = "0050_add_refunds_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "payments" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("payments")}
    if "voucher_number" not in cols:
        op.add_column(
            "payments", sa.Column("voucher_number", sa.String(32), nullable=True)
        )
    indexes = {i["name"] for i in inspector.get_indexes("payments")}
    if "ix_payments_voucher_number" not in indexes:
        op.create_index("ix_payments_voucher_number", "payments", ["voucher_number"])

    # Backfill voucher_number from gift_vouchers if gift_vouchers table exists
    if "gift_vouchers" in inspector.get_table_names():
        op.execute(
            sa.text(
                """
                UPDATE payments p
                SET voucher_number = gv.voucher_number
                FROM gift_vouchers gv
                WHERE p.voucher_id = gv.id
                  AND p.voucher_number IS NULL
                  AND gv.voucher_number IS NOT NULL
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "payments" not in inspector.get_table_names():
        return
    indexes = {i["name"] for i in inspector.get_indexes("payments")}
    if "ix_payments_voucher_number" in indexes:
        op.drop_index("ix_payments_voucher_number", table_name="payments")
    cols = {c["name"] for c in inspector.get_columns("payments")}
    if "voucher_number" in cols:
        op.drop_column("payments", "voucher_number")
