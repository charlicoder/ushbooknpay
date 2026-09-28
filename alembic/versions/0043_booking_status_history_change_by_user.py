"""Rename changed_by to change_by_user and add change_by_user_data on booking_status_history.

Revision ID: 0043_booking_status_history_change_by_user
Revises: 0042_payments_created_by_user
Create Date: 2026-09-27
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_bsh_change_by_user"
down_revision: Union[str, None] = "0042_payments_created_by_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "booking_status_history" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("booking_status_history")}
    indexes = {i["name"] for i in inspector.get_indexes("booking_status_history")}

    # 1. Rename changed_by → change_by_user (or add if missing)
    if "changed_by" in cols and "change_by_user" not in cols:
        op.alter_column(
            "booking_status_history",
            "changed_by",
            new_column_name="change_by_user",
        )
    elif "change_by_user" not in cols:
        op.add_column(
            "booking_status_history",
            sa.Column("change_by_user", sa.String(255), nullable=True),
        )

    # 2. Ensure correct type
    op.execute(
        "ALTER TABLE booking_status_history ALTER COLUMN change_by_user TYPE VARCHAR(255) "
        "USING change_by_user::text"
    )

    # 3. Migrate indexes
    if "ix_booking_status_history_changed_by" in indexes:
        op.drop_index("ix_booking_status_history_changed_by", table_name="booking_status_history")

    if "ix_booking_status_history_change_by_user" not in indexes:
        op.create_index(
            "ix_booking_status_history_change_by_user",
            "booking_status_history",
            ["change_by_user"],
        )

    # 4. Add change_by_user_data JSONB column
    cols_after = {c["name"] for c in inspector.get_columns("booking_status_history")}
    if "change_by_user_data" not in cols_after:
        op.add_column(
            "booking_status_history",
            sa.Column("change_by_user_data", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "booking_status_history" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("booking_status_history")}
    indexes = {i["name"] for i in inspector.get_indexes("booking_status_history")}

    if "change_by_user_data" in cols:
        op.drop_column("booking_status_history", "change_by_user_data")

    if "ix_booking_status_history_change_by_user" in indexes:
        op.drop_index("ix_booking_status_history_change_by_user", table_name="booking_status_history")

    if "change_by_user" in cols and "changed_by" not in cols:
        op.alter_column(
            "booking_status_history",
            "change_by_user",
            new_column_name="changed_by",
        )
        cols_after = {c["name"] for c in inspector.get_columns("booking_status_history")}
        indexes_after = {i["name"] for i in inspector.get_indexes("booking_status_history")}
        if "ix_booking_status_history_changed_by" not in indexes_after:
            op.create_index(
                "ix_booking_status_history_changed_by",
                "booking_status_history",
                ["changed_by"],
            )
