"""Add product_image_url to shop_order_items.

Revision ID: 0012_shop_item_image_url
Revises: 0011_shop_structured_address
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_shop_item_image_url"
down_revision: Union[str, None] = "0011_shop_structured_address"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shop_order_items",
        sa.Column("product_image_url", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shop_order_items", "product_image_url")
