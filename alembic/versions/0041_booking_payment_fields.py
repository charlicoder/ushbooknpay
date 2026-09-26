"""Add payment fields and rename created_by to created_by_user on bookings.

Revision ID: 0041_booking_payment_fields
Revises: 0040_shop_order_payment_fields
Create Date: 2026-09-25
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifier must be <= 32 characters for alembic_version.version_num
revision: str = "0041_booking_payment_fields"
down_revision: Union[str, None] = "0040_shop_order_payment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "bookings" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("bookings")}
    indexes = {i["name"] for i in inspector.get_indexes("bookings")}

    # 1. Rename created_by to created_by_user
    if "created_by" in cols and "created_by_user" not in cols:
        op.alter_column(
            "bookings",
            "created_by",
            new_column_name="created_by_user",
        )
    elif "created_by_user" not in cols:
        op.add_column(
            "bookings",
            sa.Column("created_by_user", sa.String(255), nullable=True),
        )

    if "ix_bookings_created_by_user" not in indexes:
        op.create_index(
            "ix_bookings_created_by_user",
            "bookings",
            ["created_by_user"],
        )

    # 2. Add payment_id
    if "payment_id" not in cols:
        op.add_column(
            "bookings",
            sa.Column("payment_id", sa.String(255), nullable=True),
        )
    if "ix_bookings_payment_id" not in indexes:
        op.create_index(
            "ix_bookings_payment_id",
            "bookings",
            ["payment_id"],
        )

    # 3. Add payment_provider
    if "payment_provider" not in cols:
        op.add_column(
            "bookings",
            sa.Column("payment_provider", sa.String(50), nullable=True),
        )

    # 4. Add payment_gateway
    if "payment_gateway" not in cols:
        op.add_column(
            "bookings",
            sa.Column("payment_gateway", sa.String(50), nullable=True),
        )

    # 5. Add payment_through
    if "payment_through" not in cols:
        op.add_column(
            "bookings",
            sa.Column("payment_through", sa.String(50), nullable=True),
        )

    # 6. Add payment_method
    if "payment_method" not in cols:
        op.add_column(
            "bookings",
            sa.Column("payment_method", sa.String(50), nullable=True),
        )

    # 7. Add payment_url
    if "payment_url" not in cols:
        op.add_column(
            "bookings",
            sa.Column("payment_url", sa.Text(), nullable=True),
        )

    # 8. Add payment_data (if not already present)
    if "payment_data" not in cols:
        op.add_column(
            "bookings",
            sa.Column(
                "payment_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    # 9. Add created_by_user_data
    if "created_by_user_data" not in cols:
        op.add_column(
            "bookings",
            sa.Column(
                "created_by_user_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "bookings" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("bookings")}
    indexes = {i["name"] for i in inspector.get_indexes("bookings")}

    if "created_by_user_data" in cols:
        op.drop_column("bookings", "created_by_user_data")

    if "payment_url" in cols:
        op.drop_column("bookings", "payment_url")

    if "payment_method" in cols:
        op.drop_column("bookings", "payment_method")

    if "payment_through" in cols:
        op.drop_column("bookings", "payment_through")

    if "payment_gateway" in cols:
        op.drop_column("bookings", "payment_gateway")

    if "payment_provider" in cols:
        op.drop_column("bookings", "payment_provider")

    if "ix_bookings_payment_id" in indexes:
        op.drop_index("ix_bookings_payment_id", table_name="bookings")

    if "payment_id" in cols:
        op.drop_column("bookings", "payment_id")

    if "ix_bookings_created_by_user" in indexes:
        op.drop_index("ix_bookings_created_by_user", table_name="bookings")

    if "created_by_user" in cols and "created_by" not in cols:
        op.alter_column(
            "bookings",
            "created_by_user",
            new_column_name="created_by",
        )
