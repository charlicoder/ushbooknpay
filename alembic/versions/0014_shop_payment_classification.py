"""Add payment_method, payment_type, payment_provider to shop_orders.

Revision ID: 0014_shop_payment_classification
Revises: 0013_shop_public_token
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_shop_payment_classification"
down_revision: Union[str, None] = "0013_shop_public_token"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("shop_orders", sa.Column("payment_method", sa.String(50), nullable=True))
    op.add_column("shop_orders", sa.Column("payment_type", sa.String(50), nullable=True))
    op.add_column("shop_orders", sa.Column("payment_provider", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("shop_orders", "payment_provider")
    op.drop_column("shop_orders", "payment_type")
    op.drop_column("shop_orders", "payment_method")
