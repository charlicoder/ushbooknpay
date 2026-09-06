"""
0004_add_unified_payment_fields
───────────────────────────────
Add unified standard payment fields to payments table:
- payment_id (String)
- transaction_id (String)
- is_paid (Boolean)
- invoice_id (String)
- invoice_value (Numeric)
- customer_name (String)
- customer_mobile (String)
- customer_email (String)
- created_date (String)
- transaction_date (String)
- payment_gateway (String)

Revision ID: 0004_add_unified_payment_fields
Revises: 0003_add_payment_audit_and_finance_fields
Create Date: 2026-08-26 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_unified_payment_fields"
down_revision = "0003_payment_audit_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" in tables:
        cols = {c["name"] for c in inspector.get_columns("payments")}

        if "payment_id" not in cols:
            op.add_column("payments", sa.Column("payment_id", sa.String(255), nullable=True))
        if "transaction_id" not in cols:
            op.add_column("payments", sa.Column("transaction_id", sa.String(255), nullable=True))
        if "is_paid" not in cols:
            op.add_column("payments", sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.text("false")))
        if "invoice_id" not in cols:
            op.add_column("payments", sa.Column("invoice_id", sa.String(255), nullable=True))
        if "invoice_value" not in cols:
            op.add_column("payments", sa.Column("invoice_value", sa.Numeric(precision=10, scale=3), nullable=True))
        if "customer_name" not in cols:
            op.add_column("payments", sa.Column("customer_name", sa.String(255), nullable=True))
        if "customer_mobile" not in cols:
            op.add_column("payments", sa.Column("customer_mobile", sa.String(50), nullable=True))
        if "customer_email" not in cols:
            op.add_column("payments", sa.Column("customer_email", sa.String(255), nullable=True))
        if "created_date" not in cols:
            op.add_column("payments", sa.Column("created_date", sa.String(100), nullable=True))
        if "transaction_date" not in cols:
            op.add_column("payments", sa.Column("transaction_date", sa.String(100), nullable=True))
        if "payment_gateway" not in cols:
            op.add_column("payments", sa.Column("payment_gateway", sa.String(100), nullable=True))

        # Indexes
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("payments")}
        if "ix_payments_payment_id" not in existing_indexes:
            op.create_index("ix_payments_payment_id", "payments", ["payment_id"])
        if "ix_payments_transaction_id" not in existing_indexes:
            op.create_index("ix_payments_transaction_id", "payments", ["transaction_id"])
        if "ix_payments_is_paid" not in existing_indexes:
            op.create_index("ix_payments_is_paid", "payments", ["is_paid"])
        if "ix_payments_invoice_id" not in existing_indexes:
            op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"])
        if "ix_payments_payment_gateway" not in existing_indexes:
            op.create_index("ix_payments_payment_gateway", "payments", ["payment_gateway"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" in tables:
        cols = {c["name"] for c in inspector.get_columns("payments")}
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("payments")}

        for ix_name in [
            "ix_payments_payment_gateway",
            "ix_payments_invoice_id",
            "ix_payments_is_paid",
            "ix_payments_transaction_id",
            "ix_payments_payment_id",
        ]:
            if ix_name in existing_indexes:
                op.drop_index(ix_name, table_name="payments")

        for col_name in [
            "payment_gateway",
            "transaction_date",
            "created_date",
            "customer_email",
            "customer_mobile",
            "customer_name",
            "invoice_value",
            "invoice_id",
            "is_paid",
            "transaction_id",
            "payment_id",
        ]:
            if col_name in cols:
                op.drop_column("payments", col_name)
