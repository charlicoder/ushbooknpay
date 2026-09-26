"""Rename created_by to created_by_user and add created_by_user_data on payments.

Revision ID: 0042_payments_created_by_user
Revises: 0041_booking_payment_fields
Create Date: 2026-09-25
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_payments_created_by_user"
down_revision: Union[str, None] = "0041_booking_payment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "payments" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("payments")}
    indexes = {i["name"] for i in inspector.get_indexes("payments")}

    # 1. Rename created_by to created_by_user or add created_by_user
    if "created_by" in cols and "created_by_user" not in cols:
        op.alter_column(
            "payments",
            "created_by",
            new_column_name="created_by_user",
        )
    elif "created_by_user" not in cols:
        op.add_column(
            "payments",
            sa.Column("created_by_user", sa.String(255), nullable=True),
        )

    # 2. Ensure created_by_user column type is VARCHAR(255)
    op.execute(
        "ALTER TABLE payments ALTER COLUMN created_by_user TYPE VARCHAR(255) USING created_by_user::text"
    )

    # 3. Migrate indexes
    if "ix_payments_created_by" in indexes:
        op.drop_index("ix_payments_created_by", table_name="payments")

    if "ix_payments_created_by_user" not in indexes:
        op.create_index(
            "ix_payments_created_by_user",
            "payments",
            ["created_by_user"],
        )

    # 4. Add created_by_user_data JSONB column
    cols_after = {c["name"] for c in inspector.get_columns("payments")}
    if "created_by_user_data" not in cols_after:
        op.add_column(
            "payments",
            sa.Column("created_by_user_data", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "payments" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("payments")}
    indexes = {i["name"] for i in inspector.get_indexes("payments")}

    if "created_by_user_data" in cols:
        op.drop_column("payments", "created_by_user_data")

    if "ix_payments_created_by_user" in indexes:
        op.drop_index("ix_payments_created_by_user", table_name="payments")

    if "created_by_user" in cols and "created_by" not in cols:
        op.alter_column(
            "payments",
            "created_by_user",
            new_column_name="created_by",
        )
        if "ix_payments_created_by" not in indexes:
            op.create_index(
                "ix_payments_created_by",
                "payments",
                ["created_by"],
            )
