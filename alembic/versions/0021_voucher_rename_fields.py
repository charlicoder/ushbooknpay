"""Rename sender_details→sender_data, recipient_details→recipient_data;
add recipient_id (UUID) and price_for_extra_time (Numeric) to gift_vouchers.

Revision ID: 0021_voucher_rename_fields
Revises: 0020_bookings_gift_voucher_type
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0021_voucher_rename_fields"
down_revision: Union[str, None] = "0020_bookings_gift_voucher_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "gift_vouchers" not in tables:
        # Table doesn't exist yet; nothing to do — migration 0016 will create it.
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    # ── Rename sender_details → sender_data ───────────────────────────────────
    if "sender_details" in existing_cols and "sender_data" not in existing_cols:
        op.alter_column("gift_vouchers", "sender_details", new_column_name="sender_data")

    # ── Rename recipient_details → recipient_data ─────────────────────────────
    if "recipient_details" in existing_cols and "recipient_data" not in existing_cols:
        op.alter_column("gift_vouchers", "recipient_details", new_column_name="recipient_data")

    # Re-inspect after renames
    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    # ── Add recipient_id (UUID, nullable) ─────────────────────────────────────
    if "recipient_id" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "recipient_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # ── Add price_for_extra_time (Numeric 10,3, nullable) ─────────────────────
    if "price_for_extra_time" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "price_for_extra_time",
                sa.Numeric(precision=10, scale=3),
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

    # Drop added columns
    if "price_for_extra_time" in existing_cols:
        op.drop_column("gift_vouchers", "price_for_extra_time")

    if "recipient_id" in existing_cols:
        op.drop_column("gift_vouchers", "recipient_id")

    # Re-inspect
    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    # Rename back recipient_data → recipient_details
    if "recipient_data" in existing_cols and "recipient_details" not in existing_cols:
        op.alter_column("gift_vouchers", "recipient_data", new_column_name="recipient_details")

    # Rename back sender_data → sender_details
    if "sender_data" in existing_cols and "sender_details" not in existing_cols:
        op.alter_column("gift_vouchers", "sender_data", new_column_name="sender_details")
