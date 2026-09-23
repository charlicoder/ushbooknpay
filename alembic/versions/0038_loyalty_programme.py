"""Create loyalty_account and loyalty_transaction tables.

Revision ID: 0038_loyalty_programme
Revises: 0037_drop_loyalty_tables
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_loyalty_programme"
down_revision: Union[str, None] = "0037_drop_loyalty_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── loyalty_account ────────────────────────────────────────────────────────
    op.create_table(
        "loyalty_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            comment="FK to the Customer in ushauth (cross-service; not DB-enforced).",
        ),
        sa.Column(
            "balance_points",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Current redeemable balance.",
        ),
        sa.Column(
            "total_earned",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Cumulative points ever earned; never decremented.",
        ),
        sa.Column(
            "total_redeemed",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="Cumulative points ever redeemed.",
        ),
        sa.Column(
            "points_expire_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Rolling expiry; reset on every earn event.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        if_not_exists=True,
    )

    # Create indexes only if the table was just created (use execute for safety)
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in insp.get_indexes("loyalty_account")}

    if "idx_loyalty_account_customer" not in existing_indexes:
        op.create_index("idx_loyalty_account_customer", "loyalty_account", ["customer_id"])
    if "idx_loyalty_account_expire" not in existing_indexes:
        op.create_index("idx_loyalty_account_expire", "loyalty_account", ["points_expire_at"])

    # ── loyalty_transaction ────────────────────────────────────────────────────
    op.create_table(
        "loyalty_transaction",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loyalty_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Denormalised for fast per-customer queries.",
        ),
        sa.Column(
            "transaction_type",
            sa.String(20),
            nullable=False,
            comment="earn | redeem | expire | adjust | cancel",
        ),
        sa.Column(
            "points",
            sa.BigInteger(),
            nullable=False,
            comment="Absolute point value; always positive.",
        ),
        sa.Column(
            "booking_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bookings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "booking_number",
            sa.String(50),
            nullable=True,
            comment="Human-readable booking reference for display.",
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by",
            sa.String(100),
            nullable=True,
            comment="Source: ushnotice, admin, system, etc.",
        ),
        if_not_exists=True,
    )

    existing_txn_indexes = {idx["name"] for idx in insp.get_indexes("loyalty_transaction")} if "loyalty_transaction" in insp.get_table_names() else set()
    if "idx_loyalty_txn_account" not in existing_txn_indexes:
        op.create_index("idx_loyalty_txn_account", "loyalty_transaction", ["account_id"])
    if "idx_loyalty_txn_customer" not in existing_txn_indexes:
        op.create_index("idx_loyalty_txn_customer", "loyalty_transaction", ["customer_id"])
    if "idx_loyalty_txn_booking" not in existing_txn_indexes:
        op.create_index("idx_loyalty_txn_booking", "loyalty_transaction", ["booking_id"])
    if "idx_loyalty_txn_created" not in existing_txn_indexes:
        op.create_index("idx_loyalty_txn_created", "loyalty_transaction", ["created_at"])



def downgrade() -> None:
    op.drop_table("loyalty_transaction")
    op.drop_table("loyalty_account")
