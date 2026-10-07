"""Add payment_number column to payments table.

Revision ID: 0046_add_payment_number
Revises: 0045_expand_reference_numbers
Create Date: 2026-10-07
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_add_payment_number"
down_revision: Union[str, None] = "0045_expand_reference_numbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("payments")}
        if "payment_number" not in cols:
            # 1. Add column as nullable initially
            op.add_column("payments", sa.Column("payment_number", sa.String(32), nullable=True))

            # 2. Backfill existing records with sequential PMT/YYYY/MM/NNNNNN values
            op.execute(
                """
                WITH numbered AS (
                    SELECT id,
                           'PMT/' || to_char(COALESCE(created_at, NOW()), 'YYYY/MM') || '/' ||
                           lpad(row_number() OVER (
                               PARTITION BY to_char(COALESCE(created_at, NOW()), 'YYYY/MM')
                               ORDER BY created_at, id
                           )::text, 6, '0') AS num
                    FROM payments
                    WHERE payment_number IS NULL
                )
                UPDATE payments
                SET payment_number = numbered.num
                FROM numbered
                WHERE payments.id = numbered.id;
                """
            )

            # 3. Enforce NOT NULL
            op.alter_column("payments", "payment_number", nullable=False)

            # 4. Create unique index
            indexes = {i["name"]: i for i in inspector.get_indexes("payments")}
            if "ix_payments_payment_number" not in indexes:
                op.create_index("ix_payments_payment_number", "payments", ["payment_number"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" in tables:
        indexes = {i["name"]: i for i in inspector.get_indexes("payments")}
        if "ix_payments_payment_number" in indexes:
            op.drop_index("ix_payments_payment_number", table_name="payments")

        cols = {c["name"]: c for c in inspector.get_columns("payments")}
        if "payment_number" in cols:
            op.drop_column("payments", "payment_number")
