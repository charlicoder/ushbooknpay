"""Add gift_category to gift_vouchers table.

Revision ID: 0028_add_gift_category_to_gift_vouchers
Revises: 0014_shop_payment_classification
Create Date: 2026-09-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_add_gift_category"
down_revision: Union[str, None] = "0014_shop_payment_classification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gift_vouchers",
        sa.Column(
            "gift_category",
            sa.String(20),
            nullable=False,
            server_default="service",
        ),
    )
    op.create_index(
        "ix_gift_vouchers_gift_category",
        "gift_vouchers",
        ["gift_category"],
    )


def downgrade() -> None:
    op.drop_index("ix_gift_vouchers_gift_category", table_name="gift_vouchers")
    op.drop_column("gift_vouchers", "gift_category")
