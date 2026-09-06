"""
0009_add_payment_indexes
─────────────────────────
Adds two indexes to the ``payments`` table that are defined in the ORM
model but were missing from the database:

  - ``ix_payments_created_at``       on ``created_at``
  - ``ix_payments_customer_reference`` on ``customer_reference``

Also suppresses the spurious server_default drift on the booking /
payment / loyalty columns that Alembic detects when comparing Python-side
``default=`` values against DB-side ``server_default=`` set by earlier
migrations.  The columns involved already have the correct Python-side
defaults in the ORM; the server_defaults in the DB are redundant and are
dropped here to keep the schema in sync with the model definitions.

Revision ID: 0009_payment_indexes
Revises: 0008_loyalty_idempotency
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0009_payment_indexes"
down_revision = "0008_loyalty_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("payments")}

    # ── Add missing indexes on payments ──────────────────────────────────────
    if "ix_payments_created_at" not in existing_indexes:
        op.create_index("ix_payments_created_at", "payments", ["created_at"])

    if "ix_payments_customer_reference" not in existing_indexes:
        op.create_index("ix_payments_customer_reference", "payments", ["customer_reference"])

    # ── Remove redundant server_defaults (bookings) ──────────────────────────
    # These were set by earlier migrations.  The ORM provides Python-side
    # defaults; the DB-side server defaults are no longer needed.
    op.alter_column("bookings", "customer_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=None, existing_nullable=True)
    op.alter_column("bookings", "branch_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=None, existing_nullable=True)
    op.alter_column("bookings", "service_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=None, existing_nullable=True)
    op.alter_column("bookings", "service_arrangement_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=None, existing_nullable=True)
    op.alter_column("bookings", "therapist_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=None, existing_nullable=True)
    op.alter_column("bookings", "extra_minutes",
                    existing_type=sa.INTEGER(), server_default=None,
                    existing_nullable=False)
    op.alter_column("bookings", "price_for_extra_minutes",
                    existing_type=sa.NUMERIC(precision=10, scale=3),
                    server_default=None, existing_nullable=False)
    op.alter_column("bookings", "booking_type",
                    existing_type=sa.VARCHAR(length=20), server_default=None,
                    existing_nullable=False)

    # ── Remove redundant server_defaults (payments) ──────────────────────────
    op.alter_column("payments", "is_paid",
                    existing_type=sa.BOOLEAN(), server_default=None,
                    existing_nullable=False)
    op.alter_column("payments", "service_charge",
                    existing_type=sa.NUMERIC(precision=10, scale=3),
                    server_default=None, existing_nullable=True)
    op.alter_column("payments", "vat_amount",
                    existing_type=sa.NUMERIC(precision=10, scale=3),
                    server_default=None, existing_nullable=True)
    op.alter_column("payments", "deposit_status",
                    existing_type=sa.VARCHAR(length=50), server_default=None,
                    existing_nullable=True)

    # ── Remove redundant server_defaults (loyalty) ────────────────────────────
    op.alter_column("promotions_loyalty_reward", "status",
                    existing_type=sa.VARCHAR(length=20), server_default=None,
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "booking_count",
                    existing_type=sa.INTEGER(), server_default=None,
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "bookings_required",
                    existing_type=sa.INTEGER(), server_default=None,
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "total_bookings",
                    existing_type=sa.INTEGER(), server_default=None,
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "total_rewards_earned",
                    existing_type=sa.INTEGER(), server_default=None,
                    existing_nullable=False)


def downgrade() -> None:
    # ── Restore server_defaults ───────────────────────────────────────────────
    op.alter_column("promotions_loyalty_tracker", "total_rewards_earned",
                    existing_type=sa.INTEGER(), server_default=sa.text("0"),
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "total_bookings",
                    existing_type=sa.INTEGER(), server_default=sa.text("0"),
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "bookings_required",
                    existing_type=sa.INTEGER(), server_default=sa.text("5"),
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_tracker", "booking_count",
                    existing_type=sa.INTEGER(), server_default=sa.text("0"),
                    existing_nullable=False)
    op.alter_column("promotions_loyalty_reward", "status",
                    existing_type=sa.VARCHAR(length=20),
                    server_default=sa.text("'available'::character varying"),
                    existing_nullable=False)
    op.alter_column("payments", "deposit_status",
                    existing_type=sa.VARCHAR(length=50),
                    server_default=sa.text("'Not Deposited'::character varying"),
                    existing_nullable=True)
    op.alter_column("payments", "vat_amount",
                    existing_type=sa.NUMERIC(precision=10, scale=3),
                    server_default=sa.text("0.000"), existing_nullable=True)
    op.alter_column("payments", "service_charge",
                    existing_type=sa.NUMERIC(precision=10, scale=3),
                    server_default=sa.text("0.000"), existing_nullable=True)
    op.alter_column("payments", "is_paid",
                    existing_type=sa.BOOLEAN(), server_default=sa.text("false"),
                    existing_nullable=False)
    op.alter_column("bookings", "booking_type",
                    existing_type=sa.VARCHAR(length=20),
                    server_default=sa.text("'branch'::character varying"),
                    existing_nullable=False)
    op.alter_column("bookings", "price_for_extra_minutes",
                    existing_type=sa.NUMERIC(precision=10, scale=3),
                    server_default=sa.text("0.000"), existing_nullable=False)
    op.alter_column("bookings", "extra_minutes",
                    existing_type=sa.INTEGER(), server_default=sa.text("0"),
                    existing_nullable=False)
    op.alter_column("bookings", "therapist_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=sa.text("'{}'::jsonb"), existing_nullable=True)
    op.alter_column("bookings", "service_arrangement_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=sa.text("'{}'::jsonb"), existing_nullable=True)
    op.alter_column("bookings", "service_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=sa.text("'{}'::jsonb"), existing_nullable=True)
    op.alter_column("bookings", "branch_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=sa.text("'{}'::jsonb"), existing_nullable=True)
    op.alter_column("bookings", "customer_data",
                    existing_type=postgresql.JSONB(astext_type=sa.Text()),
                    server_default=sa.text("'{}'::jsonb"), existing_nullable=True)

    # ── Drop indexes ──────────────────────────────────────────────────────────
    op.drop_index("ix_payments_customer_reference", table_name="payments")
    op.drop_index("ix_payments_created_at", table_name="payments")
