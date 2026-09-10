"""add redeemed_by and indexes to gift_vouchers

Revision ID: 0024_voucher_redeemed_by
Revises: 0023_voucher_payment_fields
Create Date: 2026-09-10

Adds redeemed_by column (UUID, nullable) to gift_vouchers.
Adds index on redeemed_by and created_by.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0024_voucher_redeemed_by"
down_revision: Union[str, None] = "0023_voucher_payment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("gift_vouchers")}

    if "redeemed_by" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column("redeemed_by", postgresql.UUID(as_uuid=True), nullable=True),
        )

    if "ix_gift_vouchers_redeemed_by" not in existing_indexes:
        op.create_index(
            "ix_gift_vouchers_redeemed_by",
            "gift_vouchers",
            ["redeemed_by"],
        )

    if "ix_gift_vouchers_created_by" not in existing_indexes:
        op.create_index(
            "ix_gift_vouchers_created_by",
            "gift_vouchers",
            ["created_by"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("gift_vouchers")}

    if "ix_gift_vouchers_created_by" in existing_indexes:
        op.drop_index("ix_gift_vouchers_created_by", table_name="gift_vouchers")

    if "ix_gift_vouchers_redeemed_by" in existing_indexes:
        op.drop_index("ix_gift_vouchers_redeemed_by", table_name="gift_vouchers")

    if "redeemed_by" in existing_cols:
        op.drop_column("gift_vouchers", "redeemed_by")
