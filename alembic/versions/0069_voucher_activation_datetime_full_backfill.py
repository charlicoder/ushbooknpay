"""Backfill activation_datetime = created_at for all gift_vouchers where activation_datetime is NULL.

Migration 0066 already backfilled active/redeemed/expired vouchers using
  COALESCE(delivered_at, created_at).
This migration covers every remaining row regardless of status — payment_pending,
scheduled, cancelled, and any future statuses — so that no voucher is left
without an activation_datetime.

The fallback order is:
  1. delivered_at    (set for scheduled vouchers that were activated by the scheduler)
  2. created_at      (always present — the voucher creation timestamp)

Revision ID: 0069_voucher_activation_datetime_full_backfill
Revises: 0068_voucher_desk_referenced_by_backfill
Create Date: 2026-11-10
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0069_voucher_activation_datetime_full_backfill"
down_revision: Union[str, None] = "0068_voucher_desk_referenced_by_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Set activation_datetime = COALESCE(delivered_at, created_at)
    for every gift_voucher row where activation_datetime is still NULL.
    """
    op.execute(
        """
        UPDATE gift_vouchers
           SET activation_datetime = COALESCE(delivered_at, created_at)
         WHERE activation_datetime IS NULL;
        """
    )


def downgrade() -> None:
    """Reverse: clear only the rows that were set by this migration.

    We identify them as rows where activation_datetime matches what COALESCE
    would have produced AND that were NOT already covered by 0066 (i.e. their
    status is NOT one of the statuses 0066 targeted).  This is a best-effort
    downgrade; rows originally set by 0066 are intentionally left untouched.
    """
    op.execute(
        """
        UPDATE gift_vouchers
           SET activation_datetime = NULL
         WHERE activation_datetime IS NOT NULL
           AND activation_datetime = COALESCE(delivered_at, created_at)
           AND status NOT IN ('active', 'partially_redeemed', 'redeemed', 'fulfilled', 'expired');
        """
    )

