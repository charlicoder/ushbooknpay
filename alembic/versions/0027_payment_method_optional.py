"""Make payments.payment_method and payments.payment_for optional (nullable).

Revision ID: 0027_payment_method_optional
Revises: 0026_booking_payment_fields
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027_payment_method_optional"
down_revision: str | None = "0026_booking_payment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make payment_method nullable in payments table
    op.alter_column(
        "payments",
        "payment_method",
        existing_type=sa.String(30),
        nullable=True,
    )
    # Also ensure payment_for is nullable in payments table
    op.alter_column(
        "payments",
        "payment_for",
        existing_type=sa.String(30),
        nullable=True,
    )


def downgrade() -> None:
    # Backfill default values for any NULL rows before restoring NOT NULL
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE payments SET payment_method = 'unknown' WHERE payment_method IS NULL")
    )
    conn.execute(
        sa.text("UPDATE payments SET payment_for = 'service' WHERE payment_for IS NULL")
    )
    op.alter_column(
        "payments",
        "payment_method",
        existing_type=sa.String(30),
        nullable=False,
        server_default="unknown",
    )
    op.alter_column(
        "payments",
        "payment_for",
        existing_type=sa.String(30),
        nullable=False,
        server_default="service",
    )
