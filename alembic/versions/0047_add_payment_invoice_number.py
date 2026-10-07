"""Add invoice_number column to payments table.

Revision ID: 0047_add_payment_invoice_number
Revises: 0046_add_payment_number
Create Date: 2026-10-07
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047_add_payment_invoice_number"
down_revision: Union[str, None] = "0046_add_payment_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "payments" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("payments")}
    if "invoice_number" not in cols:
        op.add_column(
            "payments", sa.Column("invoice_number", sa.String(100), nullable=True)
        )
    indexes = {i["name"] for i in inspector.get_indexes("payments")}
    if "ix_payments_invoice_number" not in indexes:
        op.create_index("ix_payments_invoice_number", "payments", ["invoice_number"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "payments" not in inspector.get_table_names():
        return
    indexes = {i["name"] for i in inspector.get_indexes("payments")}
    if "ix_payments_invoice_number" in indexes:
        op.drop_index("ix_payments_invoice_number", table_name="payments")
    cols = {c["name"] for c in inspector.get_columns("payments")}
    if "invoice_number" in cols:
        op.drop_column("payments", "invoice_number")
