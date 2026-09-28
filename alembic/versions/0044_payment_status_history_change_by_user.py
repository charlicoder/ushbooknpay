"""Add change_by_user and change_by_user_data to payment_status_history.

Revision ID: 0044_payment_status_history_change_by_user
Revises: 0043_bsh_change_by_user
Create Date: 2026-09-27
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_psh_change_by_user"
down_revision: Union[str, None] = "0043_bsh_change_by_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "payment_status_history" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("payment_status_history")}
    indexes = {i["name"] for i in inspector.get_indexes("payment_status_history")}

    # 1. Add change_by_user column if not present
    if "change_by_user" not in cols:
        op.add_column(
            "payment_status_history",
            sa.Column("change_by_user", sa.String(255), nullable=True),
        )

    # 2. Add index for change_by_user
    if "ix_payment_status_history_change_by_user" not in indexes:
        op.create_index(
            "ix_payment_status_history_change_by_user",
            "payment_status_history",
            ["change_by_user"],
        )

    # 3. Add change_by_user_data JSONB column if not present
    cols_after = {c["name"] for c in inspector.get_columns("payment_status_history")}
    if "change_by_user_data" not in cols_after:
        op.add_column(
            "payment_status_history",
            sa.Column("change_by_user_data", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "payment_status_history" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("payment_status_history")}
    indexes = {i["name"] for i in inspector.get_indexes("payment_status_history")}

    if "ix_payment_status_history_change_by_user" in indexes:
        op.drop_index("ix_payment_status_history_change_by_user", table_name="payment_status_history")

    if "change_by_user_data" in cols:
        op.drop_column("payment_status_history", "change_by_user_data")

    if "change_by_user" in cols:
        op.drop_column("payment_status_history", "change_by_user")
