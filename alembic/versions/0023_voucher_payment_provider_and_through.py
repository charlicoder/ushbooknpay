"""add payment_provider and payment_through to gift_vouchers

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-10

Adds two new optional fields to gift_vouchers:
  - payment_provider: VARCHAR(20), nullable — 'MyFatoorah' | 'DirectLink' | 'Deema' | 'Other'
  - payment_through:  VARCHAR(10), nullable — 'ushspa' | 'desk'

Both fields are nullable so existing records are unaffected.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0023_voucher_payment_fields"
down_revision: Union[str, None] = "0022_voucher_booking_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("gift_vouchers")}

    if "payment_provider" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column("payment_provider", sa.String(20), nullable=True),
        )

    if "payment_through" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column("payment_through", sa.String(10), nullable=True),
        )

    if "ix_gift_vouchers_payment_provider" not in existing_indexes:
        op.create_index(
            "ix_gift_vouchers_payment_provider",
            "gift_vouchers",
            ["payment_provider"],
        )

    if "ix_gift_vouchers_payment_through" not in existing_indexes:
        op.create_index(
            "ix_gift_vouchers_payment_through",
            "gift_vouchers",
            ["payment_through"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("gift_vouchers")}

    if "ix_gift_vouchers_payment_through" in existing_indexes:
        op.drop_index("ix_gift_vouchers_payment_through", table_name="gift_vouchers")

    if "ix_gift_vouchers_payment_provider" in existing_indexes:
        op.drop_index("ix_gift_vouchers_payment_provider", table_name="gift_vouchers")

    if "payment_through" in existing_cols:
        op.drop_column("gift_vouchers", "payment_through")

    if "payment_provider" in existing_cols:
        op.drop_column("gift_vouchers", "payment_provider")
