"""Add activation_datetime and referral tracking columns to gift_vouchers.

- activation_datetime: first time the voucher became ACTIVE (payment success).
- referenced_by / referenced_by_name / referral_number: employee whose affiliate
  link (?ref=NNNN) generated the sale.

Revision ID: 0066_voucher_activation_referral
Revises: 0065_voucher_created_by_user
Create Date: 2026-11-09
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0066_voucher_activation_referral"
down_revision: Union[str, None] = "0065_voucher_created_by_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS activation_datetime TIMESTAMPTZ;")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS referenced_by UUID;")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS referenced_by_name VARCHAR(255);")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS referral_number VARCHAR(8);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gift_vouchers_referenced_by ON gift_vouchers (referenced_by);"
    )
    # Best-effort backfill for vouchers that were already activated. The exact payment
    # time was never stored, so use delivered_at (scheduled) or created_at (paid on creation).
    op.execute(
        """
        UPDATE gift_vouchers
           SET activation_datetime = COALESCE(delivered_at, created_at)
         WHERE activation_datetime IS NULL
           AND status IN ('active', 'partially_redeemed', 'redeemed', 'fulfilled', 'expired');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gift_vouchers_referenced_by;")
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS referral_number;")
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS referenced_by_name;")
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS referenced_by;")
    op.execute("ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS activation_datetime;")

