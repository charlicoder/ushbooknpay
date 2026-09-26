"""Add payment_url and payment_data to shop_orders.

Revision ID: 0040_shop_order_payment_fields
Revises: 0039_shop_order_updates
Create Date: 2026-09-25
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifier must be <= 32 characters for alembic_version.version_num
revision: str = "0040_shop_order_payment_fields"
down_revision: Union[str, None] = "0039_shop_order_updates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "shop_orders" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("shop_orders")}

    # 1. Add payment_url
    if "payment_url" not in cols:
        op.add_column(
            "shop_orders",
            sa.Column("payment_url", sa.Text(), nullable=True),
        )

    # 2. Add payment_data
    if "payment_data" not in cols:
        op.add_column(
            "shop_orders",
            sa.Column(
                "payment_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "shop_orders" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("shop_orders")}

    if "payment_data" in cols:
        op.drop_column("shop_orders", "payment_data")

    if "payment_url" in cols:
        op.drop_column("shop_orders", "payment_url")
