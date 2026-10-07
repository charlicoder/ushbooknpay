"""Set database default timezone to Asia/Kuwait.

Every session (including DBeaver/pgAdmin/psql) then shows timestamptz values
(created_at / updated_at) as +03:00. Stored instants are unchanged.

Revision ID: 0048_set_db_timezone_kuwait
Revises: 0047_add_payment_invoice_number
Create Date: 2026-10-07
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0048_set_db_timezone_kuwait"
down_revision: Union[str, None] = "0047_add_payment_invoice_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN EXECUTE format("
        "'ALTER DATABASE %I SET timezone TO ''Asia/Kuwait''', current_database()); "
        "END $$;"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN EXECUTE format("
        "'ALTER DATABASE %I RESET timezone', current_database()); END $$;"
    )
