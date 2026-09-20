"""Add digital_product_data and is_digital_gift_opened to gift_vouchers.

Revision ID: 0031_digital_product_data
Revises: 0030_voucher_service_id_optional
Create Date: 2026-09-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0031_digital_product_data"
down_revision: Union[str, None] = "0030_voucher_service_id_optional"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from alembic import context
    is_offline = context.is_offline_mode()

    if not is_offline:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        existing_cols = {col["name"] for col in inspector.get_columns("gift_vouchers")}
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}
    else:
        existing_cols = set()
        existing_indexes = set()

    if "digital_product_data" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "digital_product_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )
    if "is_digital_gift_opened" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "is_digital_gift_opened",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if "ix_gift_vouchers_is_digital_gift_opened" not in existing_indexes:
        op.create_index(
            "ix_gift_vouchers_is_digital_gift_opened",
            "gift_vouchers",
            ["is_digital_gift_opened"],
        )


def downgrade() -> None:
    op.drop_index("ix_gift_vouchers_is_digital_gift_opened", table_name="gift_vouchers")
    op.drop_column("gift_vouchers", "is_digital_gift_opened")
    op.drop_column("gift_vouchers", "digital_product_data")
