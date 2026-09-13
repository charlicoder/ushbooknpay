"""Add public_token, token_expires_at to shop_orders; shrink tracking_code to 6 chars.

Revision ID: 0013_shop_public_token
Revises: 0012_shop_item_image_url
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_shop_public_token"
down_revision: Union[str, None] = "0012_shop_item_image_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new tracking columns
    op.add_column(
        "shop_orders",
        sa.Column("public_token", sa.String(64), nullable=True),  # nullable first for existing rows
    )
    op.add_column(
        "shop_orders",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Back-fill public_token for existing rows using md5(random()) — no pgcrypto needed
    op.execute(
        """
        UPDATE shop_orders
        SET
            public_token      = md5(random()::text || id::text),
            token_expires_at  = now() + interval '1 week'
        WHERE public_token IS NULL
        """
    )

    # Now make public_token NOT NULL and add unique constraint + index
    op.alter_column("shop_orders", "public_token", nullable=False)
    op.create_unique_constraint("uq_shop_orders_public_token", "shop_orders", ["public_token"])
    op.create_index("ix_shop_orders_public_token", "shop_orders", ["public_token"])

    # Widen tracking_code column to accommodate both old (8-12 char) and new (6 digit) codes.
    # We keep existing data as-is — no breakage for already-placed orders.
    op.alter_column(
        "shop_orders",
        "tracking_code",
        type_=sa.String(12),   # keep as 12 so old data still fits; service now writes 6-digit
        existing_type=sa.String(12),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_index("ix_shop_orders_public_token", table_name="shop_orders")
    op.drop_constraint("uq_shop_orders_public_token", "shop_orders", type_="unique")
    op.drop_column("shop_orders", "token_expires_at")
    op.drop_column("shop_orders", "public_token")
