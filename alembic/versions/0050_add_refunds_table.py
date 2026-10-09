"""Add amount_refunded to payments and create refunds table.

Revision ID: 0050_add_refunds_table
Revises: 0049_add_booking_invoice_number
Create Date: 2026-10-08
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0050_add_refunds_table"
down_revision: Union[str, None] = "0049_add_booking_invoice_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. Add amount_refunded to payments if missing
    if "payments" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("payments")}
        if "amount_refunded" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "amount_refunded",
                    sa.Numeric(precision=10, scale=3),
                    nullable=False,
                    server_default="0.000",
                ),
            )

    # 2. Create refunds table if missing
    if "refunds" not in existing_tables:
        op.create_table(
            "refunds",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("refund_number", sa.String(32), nullable=False, unique=True),
            sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("booking_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "payment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("payments.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("invoice_number", sa.String(100), nullable=True),
            sa.Column("credit_note_number", sa.String(100), nullable=True),
            sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("customer_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("branch_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("refund_type", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("refund_method", sa.String(30), nullable=False, server_default="cash"),
            sa.Column("status", sa.String(30), nullable=False, server_default="completed"),
            sa.Column("requested_amount", sa.Numeric(precision=10, scale=3), nullable=False),
            sa.Column(
                "cancellation_fee",
                sa.Numeric(precision=10, scale=3),
                nullable=False,
                server_default="0.000",
            ),
            sa.Column("refunded_amount", sa.Numeric(precision=10, scale=3), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="KWD"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("customer_confirmation", sa.Text(), nullable=True),
            sa.Column("reference_number", sa.String(255), nullable=True),
            sa.Column("payment_gateway", sa.String(50), nullable=True),
            sa.Column("gateway_transaction_id", sa.String(255), nullable=True),
            sa.Column("gateway_refund_id", sa.String(255), nullable=True),
            sa.Column("gateway_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("processed_by", sa.String(255), nullable=True),
            sa.Column("processed_by_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

        op.create_index("ix_refunds_refund_number", "refunds", ["refund_number"])
        op.create_index("ix_refunds_booking_id", "refunds", ["booking_id"])
        op.create_index("ix_refunds_payment_id", "refunds", ["payment_id"])
        op.create_index("ix_refunds_customer_id", "refunds", ["customer_id"])
        op.create_index("ix_refunds_branch_id", "refunds", ["branch_id"])
        op.create_index("ix_refunds_refund_type", "refunds", ["refund_type"])
        op.create_index("ix_refunds_refund_method", "refunds", ["refund_method"])
        op.create_index("ix_refunds_status", "refunds", ["status"])
        op.create_index("ix_refunds_created_at", "refunds", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "refunds" in existing_tables:
        op.drop_table("refunds")

    if "payments" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("payments")}
        if "amount_refunded" in cols:
            op.drop_column("payments", "amount_refunded")
