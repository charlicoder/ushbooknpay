"""Indexes for the ushdesk gift voucher list (paged, filtered, searched in the database).

Revision ID: 0067_voucher_desk_list_indexes
Revises: 0066_voucher_activation_referral
Create Date: 2026-11-09
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0067_voucher_desk_list_indexes"
down_revision: Union[str, None] = "0066_voucher_activation_referral"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Newest-first paging with a status filter
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gift_vouchers_created_at_id_desc "
        "ON gift_vouchers (created_at DESC, id DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gift_vouchers_status_created_at "
        "ON gift_vouchers (status, created_at DESC);"
    )
    # Fast "contains" search (needs pg_trgm; skipped silently when the extension can't be created)
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            CREATE INDEX IF NOT EXISTS ix_gift_vouchers_secret_code_trgm
                ON gift_vouchers USING gin (lower(secret_code) gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS ix_gift_vouchers_recipient_phone_trgm
                ON gift_vouchers USING gin (recipient_phone gin_trgm_ops);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_trgm search indexes skipped: %', SQLERRM;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gift_vouchers_recipient_phone_trgm;")
    op.execute("DROP INDEX IF EXISTS ix_gift_vouchers_secret_code_trgm;")
    op.execute("DROP INDEX IF EXISTS ix_gift_vouchers_status_created_at;")
    op.execute("DROP INDEX IF EXISTS ix_gift_vouchers_created_at_id_desc;")
