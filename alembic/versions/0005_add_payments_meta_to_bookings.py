"""
0005_add_payments_meta_to_bookings
──────────────────────────────────
Add payments_meta (JSONB) column to bookings table to store payment details snapshot.

Revision ID: 0005_add_payments_meta_to_bookings
Revises: 0004_add_unified_payment_fields
Create Date: 2026-08-26 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0005_booking_payments_meta"
down_revision = "0004_unified_payment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" in tables:
        cols = {c["name"] for c in inspector.get_columns("bookings")}
        if "payments_meta" not in cols:
            op.add_column(
                "bookings",
                sa.Column(
                    "payments_meta",
                    postgresql.JSONB(astext_type=sa.Text()),
                    nullable=True,
                    server_default=sa.text("'{}'::jsonb"),
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" in tables:
        cols = {c["name"] for c in inspector.get_columns("bookings")}
        if "payments_meta" in cols:
            op.drop_column("bookings", "payments_meta")
