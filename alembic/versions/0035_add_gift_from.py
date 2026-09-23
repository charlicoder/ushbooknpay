"""Add gift_from to gift_vouchers table.

Adds an optional text column `gift_from` to store the name/signature of the
gift giver (e.g. "From your bestie Sarah").

The column is nullable so existing rows are unaffected.

Revision ID: 0035_add_gift_from
Revises: 0034_add_voucher_number
Create Date: 2026-09-22

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035_add_gift_from"
down_revision: Union[str, None] = "0034_add_voucher_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add gift_from column (nullable Text)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    if "gift_from" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "gift_from",
                sa.Text(),
                nullable=True,
                server_default=None,
            ),
        )


def downgrade() -> None:
    """Drop gift_from column."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = set(inspector.get_table_names())
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    if "gift_from" in existing_cols:
        op.drop_column("gift_vouchers", "gift_from")
