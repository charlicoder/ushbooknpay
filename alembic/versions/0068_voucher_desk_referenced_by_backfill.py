"""Backfill referenced_by = created_by for desk-created vouchers where referenced_by is NULL.

When a gift voucher is created from ushdesk (payment_through = 'desk'), the staff
member who processed the sale is now automatically recorded as referenced_by.
This migration applies the same logic retroactively to all existing vouchers that
were created via desk before this change was introduced.

Only rows that satisfy ALL conditions below are updated:
  - payment_through = 'desk'   → sold at the reception desk
  - referenced_by IS NULL       → not already attributed to anyone
  - created_by IS NOT NULL      → we know who created it

Revision ID: 0068_voucher_desk_referenced_by_backfill
Revises: 0067_voucher_desk_list_indexes
Create Date: 2026-11-10
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0068_voucher_desk_referenced_by_backfill"
down_revision: Union[str, None] = "0067_voucher_desk_list_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill referenced_by and referenced_by_name from created_by / created_by_name
    for all desk-created vouchers that have no referral attribution yet.
    """
    op.execute(
        """
        UPDATE gift_vouchers
           SET referenced_by      = created_by,
               referenced_by_name = created_by_name
         WHERE payment_through = 'desk'
           AND referenced_by IS NULL
           AND created_by IS NOT NULL;
        """
    )


def downgrade() -> None:
    """Reverse: clear the backfilled attributed values.

    We only clear rows whose referenced_by still matches created_by AND whose
    referenced_by_name still matches created_by_name so that any manually-set
    referral attributions (e.g. from real affiliate codes added later) are not
    accidentally removed.
    """
    op.execute(
        """
        UPDATE gift_vouchers
           SET referenced_by      = NULL,
               referenced_by_name = NULL
         WHERE payment_through = 'desk'
           AND referenced_by  = created_by
           AND (
               referenced_by_name = created_by_name
               OR (referenced_by_name IS NULL AND created_by_name IS NULL)
           );
        """
    )

