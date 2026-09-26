"""Update shop_orders: rename payment_type to payment_through, add order_requested_by_user and order_requested_by_user_data.

Revision ID: 0039_shop_order_updates
Revises: 0038_loyalty_programme
Create Date: 2026-09-25
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifier must be <= 32 characters for alembic_version.version_num
revision: str = "0039_shop_order_updates"
down_revision: Union[str, None] = "0038_loyalty_programme"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "shop_orders" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("shop_orders")}
    indexes = {i["name"] for i in inspector.get_indexes("shop_orders")}

    # 1. Rename payment_type to payment_through
    if "payment_type" in cols and "payment_through" not in cols:
        op.alter_column(
            "shop_orders",
            "payment_type",
            new_column_name="payment_through",
        )
    elif "payment_through" not in cols:
        op.add_column(
            "shop_orders",
            sa.Column("payment_through", sa.String(50), nullable=True),
        )

    # 2. Add order_requested_by_user and index
    if "order_requested_by_user" not in cols:
        op.add_column(
            "shop_orders",
            sa.Column(
                "order_requested_by_user",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    if "ix_shop_orders_order_requested_by_user" not in indexes:
        op.create_index(
            "ix_shop_orders_order_requested_by_user",
            "shop_orders",
            ["order_requested_by_user"],
        )

    # 3. Add order_requested_by_user_data
    if "order_requested_by_user_data" not in cols:
        op.add_column(
            "shop_orders",
            sa.Column(
                "order_requested_by_user_data",
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
    indexes = {i["name"] for i in inspector.get_indexes("shop_orders")}

    if "order_requested_by_user_data" in cols:
        op.drop_column("shop_orders", "order_requested_by_user_data")

    if "ix_shop_orders_order_requested_by_user" in indexes:
        op.drop_index(
            "ix_shop_orders_order_requested_by_user",
            table_name="shop_orders",
        )

    if "order_requested_by_user" in cols:
        op.drop_column("shop_orders", "order_requested_by_user")

    if "payment_through" in cols and "payment_type" not in cols:
        op.alter_column(
            "shop_orders",
            "payment_through",
            new_column_name="payment_type",
        )
