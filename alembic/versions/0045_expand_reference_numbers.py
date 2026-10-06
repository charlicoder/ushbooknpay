"""Expand reference number columns (booking_number, voucher_number, order_number) to VARCHAR(32).

Revision ID: 0045_expand_reference_numbers
Revises: 0044_psh_change_by_user
Create Date: 2026-10-06
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_expand_reference_numbers"
down_revision: Union[str, None] = "0044_psh_change_by_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("bookings")}
        if "booking_number" in cols:
            op.alter_column(
                "bookings",
                "booking_number",
                type_=sa.String(32),
                existing_type=sa.String(20),
                existing_nullable=True,
            )

    if "gift_vouchers" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("gift_vouchers")}
        if "voucher_number" in cols:
            op.alter_column(
                "gift_vouchers",
                "voucher_number",
                type_=sa.String(32),
                existing_type=sa.String(20),
                existing_nullable=True,
            )

    if "shop_orders" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("shop_orders")}
        if "order_number" in cols:
            op.alter_column(
                "shop_orders",
                "order_number",
                type_=sa.String(32),
                existing_type=sa.String(30),
                existing_nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("bookings")}
        if "booking_number" in cols:
            op.alter_column(
                "bookings",
                "booking_number",
                type_=sa.String(20),
                existing_type=sa.String(32),
                existing_nullable=True,
            )

    if "gift_vouchers" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("gift_vouchers")}
        if "voucher_number" in cols:
            op.alter_column(
                "gift_vouchers",
                "voucher_number",
                type_=sa.String(20),
                existing_type=sa.String(32),
                existing_nullable=True,
            )

    if "shop_orders" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("shop_orders")}
        if "order_number" in cols:
            op.alter_column(
                "shop_orders",
                "order_number",
                type_=sa.String(30),
                existing_type=sa.String(32),
                existing_nullable=False,
            )
