"""Add ordered_items, delivery_status, and delivery_address to gift_vouchers.

Revision ID: 0029_add_delivery_and_items
Revises: 0028_add_gift_category
Create Date: 2026-09-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0029_add_delivery_and_items"
down_revision: Union[str, None] = "0028_add_gift_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gift_vouchers",
        sa.Column(
            "ordered_items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "gift_vouchers",
        sa.Column(
            "delivery_status",
            sa.String(20),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_gift_vouchers_delivery_status",
        "gift_vouchers",
        ["delivery_status"],
    )
    op.add_column(
        "gift_vouchers",
        sa.Column(
            "delivery_address",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("gift_vouchers", "delivery_address")
    op.drop_index("ix_gift_vouchers_delivery_status", table_name="gift_vouchers")
    op.drop_column("gift_vouchers", "delivery_status")
    op.drop_column("gift_vouchers", "ordered_items")
