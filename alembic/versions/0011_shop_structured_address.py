"""Replace shop_orders.delivery_address with structured address fields.

Revision ID: 0011_shop_structured_address
Revises: 0010_shop_tables
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_shop_structured_address"
down_revision: Union[str, None] = "0010_shop_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add structured address columns and contact_number
    op.add_column("shop_orders", sa.Column("contact_number", sa.String(30), nullable=False, server_default=""))
    op.add_column("shop_orders", sa.Column("area",        sa.String(100), nullable=False, server_default=""))
    op.add_column("shop_orders", sa.Column("block",       sa.String(50),  nullable=False, server_default=""))
    op.add_column("shop_orders", sa.Column("street",      sa.String(100), nullable=False, server_default=""))
    op.add_column("shop_orders", sa.Column("building_no", sa.String(50),  nullable=False, server_default=""))
    op.add_column("shop_orders", sa.Column("floor",       sa.String(50),  nullable=True))
    op.add_column("shop_orders", sa.Column("apartment",   sa.String(50),  nullable=True))
    op.add_column("shop_orders", sa.Column("city",        sa.String(100), nullable=True))

    # Migrate existing data: copy delivery_address into 'area' so no data is lost
    op.execute(
        "UPDATE shop_orders SET area = delivery_address WHERE area = '' AND delivery_address IS NOT NULL"
    )

    # Drop the old flat text column
    op.drop_column("shop_orders", "delivery_address")


def downgrade() -> None:
    # Re-add delivery_address and populate from structured fields
    op.add_column("shop_orders", sa.Column("delivery_address", sa.Text, nullable=False, server_default=""))
    op.execute(
        "UPDATE shop_orders SET delivery_address = "
        "CONCAT_WS(', ', NULLIF(area,''), NULLIF(block,''), NULLIF(street,''), "
        "NULLIF(building_no,''), floor, apartment, city)"
    )
    op.drop_column("shop_orders", "city")
    op.drop_column("shop_orders", "apartment")
    op.drop_column("shop_orders", "floor")
    op.drop_column("shop_orders", "building_no")
    op.drop_column("shop_orders", "street")
    op.drop_column("shop_orders", "block")
    op.drop_column("shop_orders", "area")
    op.drop_column("shop_orders", "contact_number")
