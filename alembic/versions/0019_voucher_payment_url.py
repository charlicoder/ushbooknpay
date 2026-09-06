"""Add payment_url column to gift_vouchers table.

Stores the payment gateway checkout/redirect URL (e.g. MyFatoorah invoice URL
or hosted payment session) where the customer completes payment.

Revision ID: 0019_voucher_payment_url
Revises: 0018_voucher_payment_id_str
Create Date: 2026-09-04 00:00:00.000000
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0019_voucher_payment_url"
down_revision: Union[str, None] = "0018_voucher_payment_id_str"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    # ── Add payment_url (nullable Text) ──────────────────────────────────────
    if "payment_url" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "payment_url",
                sa.Text(),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("gift_vouchers")}

    if "payment_url" in existing_cols:
        op.drop_column("gift_vouchers", "payment_url")
