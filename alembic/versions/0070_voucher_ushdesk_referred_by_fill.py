"""Backfill referenced_by = created_by for ushdesk vouchers where referenced_by is NULL.

Revision ID: 0070_voucher_ushdesk_ref_fill
Revises: 0069_voucher_activation_fill
Create Date: 2026-11-10
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0070_voucher_ushdesk_ref_fill"
down_revision: Union[str, None] = "0069_voucher_activation_fill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill referenced_by and referenced_by_name from created_by / created_by_name
    for all desk/ushdesk vouchers that have no referral attribution yet.
    """
    op.execute(
        """
        UPDATE gift_vouchers
           SET referenced_by      = created_by,
               referenced_by_name = created_by_name
         WHERE LOWER(payment_through) IN ('desk', 'ushdesk')
           AND referenced_by IS NULL
           AND created_by IS NOT NULL;
        """
    )


def downgrade() -> None:
    """Reverse: clear backfilled attributed values for desk/ushdesk vouchers."""
    op.execute(
        """
        UPDATE gift_vouchers
           SET referenced_by      = NULL,
               referenced_by_name = NULL
         WHERE LOWER(payment_through) IN ('desk', 'ushdesk')
           AND referenced_by = created_by
           AND (referenced_by_name = created_by_name OR (referenced_by_name IS NULL AND created_by_name IS NULL));
        """
    )

