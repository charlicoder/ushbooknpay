"""Make booking_id optional and add voucher_id, voucher_data, payment_for to payments table.

Revision ID: 0015_payment_voucher_fields
Revises: 0014_add_created_by
Create Date: 2026-09-03 00:00:00.000000

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015_payment_voucher_fields"
down_revision: Union[str, None] = "0014_add_created_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("payments")}
    indexes = {idx["name"] for idx in inspector.get_indexes("payments")}

    # ── 1. Drop foreign key on booking_id ─────────────────────────────────
    fks = inspector.get_foreign_keys("payments")
    for fk in fks:
        if "booking_id" in fk.get("constrained_columns", []):
            fk_name = fk.get("name")
            if fk_name:
                op.drop_constraint(fk_name, "payments", type_="foreignkey")

    # ── 2. Make booking_id nullable ───────────────────────────────────────
    if "booking_id" in cols:
        op.alter_column(
            "payments",
            "booking_id",
            nullable=True,
            existing_type=postgresql.UUID(as_uuid=True),
        )

    # ── 3. Add voucher_id column ──────────────────────────────────────────
    if "voucher_id" not in cols:
        op.add_column(
            "payments",
            sa.Column("voucher_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        if "ix_payments_voucher_id" not in indexes:
            op.create_index("ix_payments_voucher_id", "payments", ["voucher_id"])

    # ── 4. Add voucher_data column ────────────────────────────────────────
    if "voucher_data" not in cols:
        op.add_column(
            "payments",
            sa.Column(
                "voucher_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    # ── 5. Add payment_for column ─────────────────────────────────────────
    if "payment_for" not in cols:
        op.add_column(
            "payments",
            sa.Column(
                "payment_for",
                sa.String(50),
                nullable=False,
                server_default="service",
            ),
        )
        if "ix_payments_payment_for" not in indexes:
            op.create_index("ix_payments_payment_for", "payments", ["payment_for"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("payments")}
    indexes = {idx["name"] for idx in inspector.get_indexes("payments")}

    # ── Reverse: drop payment_for ─────────────────────────────────────────
    if "ix_payments_payment_for" in indexes:
        op.drop_index("ix_payments_payment_for", table_name="payments")

    if "payment_for" in cols:
        op.drop_column("payments", "payment_for")

    # ── Reverse: drop voucher_data ────────────────────────────────────────
    if "voucher_data" in cols:
        op.drop_column("payments", "voucher_data")

    # ── Reverse: drop voucher_id ──────────────────────────────────────────
    if "ix_payments_voucher_id" in indexes:
        op.drop_index("ix_payments_voucher_id", table_name="payments")

    if "voucher_id" in cols:
        op.drop_column("payments", "voucher_id")

    # ── Reverse: restore NOT NULL and foreign key on booking_id ──────────
    if "booking_id" in cols:
        # Note: can only restore NOT NULL and FK if there are no nulls
        op.alter_column(
            "payments",
            "booking_id",
            nullable=False,
            existing_type=postgresql.UUID(as_uuid=True),
        )
        fks = inspector.get_foreign_keys("payments")
        existing_booking_fks = [
            fk for fk in fks if "booking_id" in fk.get("constrained_columns", [])
        ]
        if not existing_booking_fks:
            op.create_foreign_key(
                "fk_payments_booking_id_bookings",
                "payments",
                "bookings",
                ["booking_id"],
                ["id"],
                ondelete="CASCADE",
            )
