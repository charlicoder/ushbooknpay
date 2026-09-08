"""Add booking_data (JSONB) column to gift_vouchers.

Revision ID: 0022_voucher_booking_data
Revises: 0021_voucher_rename_fields
Create Date: 2026-09-07 00:00:00.000000
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0022_voucher_booking_data"
down_revision: Union[str, None] = "0021_voucher_rename_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    # ── Add booking_data (JSONB, nullable) ───────────────────────────────────
    if "booking_data" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "booking_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    if "booking_data" in existing_cols:
        op.drop_column("gift_vouchers", "booking_data")
