"""Alter gift_vouchers.payment_id from UUID to VARCHAR(100) and add payment_data JSONB.

The payment_id column originally stored a UUID (no FK). It is now a plain
VARCHAR(100) to accommodate gateway-specific reference strings such as
"100624710000000255" (MyFatoorah, Tap, KNET, etc.).

payment_data is a new JSONB column that stores the full payment provider
response snapshot alongside the reference string for complete audit records.

Revision ID: 0018_voucher_payment_id_str
Revises: 0017_bookings_add_voucher_fields
Create Date: 2026-09-04 00:00:00.000000
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic (must be <= 32 chars for alembic_version table).
revision: str = "0018_voucher_payment_id_str"
down_revision: Union[str, None] = "0017_bookings_add_voucher_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"]: c for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}

    # ── 1. Drop the old ix_gift_vouchers_payment_id index (UUID-typed column) ─
    # We must drop and recreate the index because the column type changes.
    if "ix_gift_vouchers_payment_id" in existing_indexes:
        op.drop_index("ix_gift_vouchers_payment_id", table_name="gift_vouchers")

    # ── 2. Alter payment_id: UUID → VARCHAR(100) ──────────────────────────────
    # PostgreSQL requires USING cast to convert uuid values to text.
    if "payment_id" in existing_cols:
        col_type = str(existing_cols["payment_id"]["type"]).upper()
        if "UUID" in col_type:
            op.execute(
                "ALTER TABLE gift_vouchers "
                "ALTER COLUMN payment_id TYPE VARCHAR(100) USING payment_id::text"
            )

    # ── 3. Re-create the index on the new VARCHAR column ─────────────────────
    if "ix_gift_vouchers_payment_id" not in {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}:
        op.create_index("ix_gift_vouchers_payment_id", "gift_vouchers", ["payment_id"])

    # ── 4. Add payment_data JSONB column ──────────────────────────────────────
    if "payment_data" not in existing_cols:
        op.add_column(
            "gift_vouchers",
            sa.Column(
                "payment_data",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gift_vouchers" not in tables:
        return

    existing_cols = {c["name"]: c for c in inspector.get_columns("gift_vouchers")}
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}

    # ── 1. Drop payment_data ──────────────────────────────────────────────────
    if "payment_data" in existing_cols:
        op.drop_column("gift_vouchers", "payment_data")

    # ── 2. Drop VARCHAR index ─────────────────────────────────────────────────
    if "ix_gift_vouchers_payment_id" in existing_indexes:
        op.drop_index("ix_gift_vouchers_payment_id", table_name="gift_vouchers")

    # ── 3. Revert payment_id: VARCHAR(100) → UUID ─────────────────────────────
    # Only rows with valid UUID strings survive. Non-UUID values will cause
    # a cast error — acceptable for downgrade (data loss is expected).
    if "payment_id" in existing_cols:
        col_type = str(existing_cols["payment_id"]["type"]).upper()
        if "UUID" not in col_type:
            op.execute(
                "ALTER TABLE gift_vouchers "
                "ALTER COLUMN payment_id TYPE UUID USING payment_id::uuid"
            )

    # ── 4. Recreate UUID index ────────────────────────────────────────────────
    op.create_index("ix_gift_vouchers_payment_id", "gift_vouchers", ["payment_id"])
