"""Add shop_orders, shop_order_items, shop_order_status_history tables.

Revision ID: 0010_shop_tables
Revises: 0027_payment_method_optional
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010_shop_tables"
down_revision: Union[str, None] = "0027_payment_method_optional"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tables may already exist if they were applied under an earlier revision stamp.
    # Use raw SQL with IF NOT EXISTS so this migration is safe to re-run.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "shop_orders" in inspector.get_table_names():
        # Tables exist — nothing to do; migration recorded as applied.
        return

    # ── shop_orders ────────────────────────────────────────────────────
    op.create_table(
        "shop_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_number", sa.String(30), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("customer_phone", sa.String(30), nullable=False, server_default=""),
        sa.Column("delivery_address", sa.Text, nullable=False),
        sa.Column("delivery_notes", sa.Text, nullable=True),
        sa.Column("delivery_status", sa.String(20), nullable=False, server_default="ordered"),
        sa.Column("tracking_code", sa.String(12), nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
        sa.Column("discount", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
        sa.Column("total_amount", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KWD"),
        sa.Column("payment_status", sa.String(20), nullable=False, server_default="not_initiated"),
        sa.Column("internal_notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_shop_orders_order_number", "shop_orders", ["order_number"])
    op.create_unique_constraint("uq_shop_orders_tracking_code", "shop_orders", ["tracking_code"])
    op.create_index("ix_shop_orders_order_number", "shop_orders", ["order_number"])
    op.create_index("ix_shop_orders_tracking_code", "shop_orders", ["tracking_code"])
    op.create_index("ix_shop_orders_customer_id", "shop_orders", ["customer_id"])
    op.create_index("ix_shop_orders_delivery_status", "shop_orders", ["delivery_status"])
    op.create_index("ix_shop_orders_payment_status", "shop_orders", ["payment_status"])
    op.create_index("ix_shop_orders_created_at", "shop_orders", ["created_at"])
    op.create_index(
        "ix_shop_orders_customer_status",
        "shop_orders",
        ["customer_id", "delivery_status"],
    )

    # ── shop_order_items ───────────────────────────────────────────────
    op.create_table(
        "shop_order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shop_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("product_name_ar", sa.String(200), nullable=False, server_default=""),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("line_total", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KWD"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_shop_order_items_order_id", "shop_order_items", ["order_id"])
    op.create_index("ix_shop_order_items_product", "shop_order_items", ["product_id"])

    # ── shop_order_status_history ──────────────────────────────────────
    op.create_table(
        "shop_order_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shop_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(20), nullable=False),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("changed_by", sa.String(100), nullable=False, server_default="system"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_shop_order_status_history_order_id", "shop_order_status_history", ["order_id"])


def downgrade() -> None:
    op.drop_table("shop_order_status_history")
    op.drop_table("shop_order_items")
    op.drop_table("shop_orders")
