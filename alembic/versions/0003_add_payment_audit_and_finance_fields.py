"""
0003_add_payment_audit_and_finance_fields
────────────────────────────────────────
Add auditing, accounting, and finance dashboard fields to payments table:
- customer_data (JSONB)
- booking_data (JSONB)
- gateway_name (String)
- invoice_reference (String)
- customer_reference (String)
- reference_id (String)
- track_id (String)
- authorization_id (String)
- payment_id_gateway (String)
- service_charge (Numeric)
- vat_amount (Numeric)
- due_deposit (Numeric)
- deposit_status (String)
- ip_address (String)
- country (String)
- card_info (JSONB)
- paid_at (DateTime)
- metadata (JSONB)

Revision ID: 0003_add_payment_audit_and_finance_fields
Revises: 0002_booking_refactor_json_data
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0003_payment_audit_fields"
down_revision = "0002_booking_refactor_json_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" in tables:
        cols = {c["name"] for c in inspector.get_columns("payments")}

        # ── 1. Snapshots ───────────────────────────────────────────────────
        if "customer_data" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "customer_data",
                    postgresql.JSONB(astext_type=sa.Text()),
                    nullable=True,
                    server_default=sa.text("'{}'::jsonb"),
                ),
            )
        if "booking_data" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "booking_data",
                    postgresql.JSONB(astext_type=sa.Text()),
                    nullable=True,
                    server_default=sa.text("'{}'::jsonb"),
                ),
            )

        # ── 2. Financial Charges & Deposits ────────────────────────────────
        if "service_charge" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "service_charge",
                    sa.Numeric(precision=10, scale=3),
                    nullable=True,
                    server_default="0.000",
                ),
            )
        if "vat_amount" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "vat_amount",
                    sa.Numeric(precision=10, scale=3),
                    nullable=True,
                    server_default="0.000",
                ),
            )
        if "due_deposit" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "due_deposit",
                    sa.Numeric(precision=10, scale=3),
                    nullable=True,
                ),
            )
        if "deposit_status" not in cols:
            op.add_column(
                "payments",
                sa.Column(
                    "deposit_status",
                    sa.String(50),
                    nullable=True,
                    server_default="Not Deposited",
                ),
            )

        # ── 3. Gateway Identifiers ─────────────────────────────────────────
        if "gateway_name" not in cols:
            op.add_column(
                "payments",
                sa.Column("gateway_name", sa.String(50), nullable=True),
            )
        if "invoice_reference" not in cols:
            op.add_column(
                "payments",
                sa.Column("invoice_reference", sa.String(255), nullable=True),
            )
        if "customer_reference" not in cols:
            op.add_column(
                "payments",
                sa.Column("customer_reference", sa.String(255), nullable=True),
            )
        if "reference_id" not in cols:
            op.add_column(
                "payments",
                sa.Column("reference_id", sa.String(255), nullable=True),
            )
        if "track_id" not in cols:
            op.add_column(
                "payments",
                sa.Column("track_id", sa.String(255), nullable=True),
            )
        if "authorization_id" not in cols:
            op.add_column(
                "payments",
                sa.Column("authorization_id", sa.String(255), nullable=True),
            )
        if "payment_id_gateway" not in cols:
            op.add_column(
                "payments",
                sa.Column("payment_id_gateway", sa.String(255), nullable=True),
            )

        # ── 4. Network & Device Audit ──────────────────────────────────────
        if "ip_address" not in cols:
            op.add_column(
                "payments",
                sa.Column("ip_address", sa.String(45), nullable=True),
            )
        if "country" not in cols:
            op.add_column(
                "payments",
                sa.Column("country", sa.String(100), nullable=True),
            )
        if "card_info" not in cols:
            op.add_column(
                "payments",
                sa.Column("card_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            )
        if "paid_at" not in cols:
            op.add_column(
                "payments",
                sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            )
        if "metadata" not in cols:
            op.add_column(
                "payments",
                sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            )

        # ── 5. Indexes ─────────────────────────────────────────────────────
        indexes = {i["name"] for i in inspector.get_indexes("payments")}
        if "ix_payments_provider_reference" not in indexes:
            op.create_index("ix_payments_provider_reference", "payments", ["provider_reference"])
        if "ix_payments_invoice_reference" not in indexes:
            op.create_index("ix_payments_invoice_reference", "payments", ["invoice_reference"])
        if "ix_payments_reference_id" not in indexes:
            op.create_index("ix_payments_reference_id", "payments", ["reference_id"])
        if "ix_payments_track_id" not in indexes:
            op.create_index("ix_payments_track_id", "payments", ["track_id"])
        if "ix_payments_gateway_name" not in indexes:
            op.create_index("ix_payments_gateway_name", "payments", ["gateway_name"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "payments" in tables:
        cols = {c["name"] for c in inspector.get_columns("payments")}
        indexes = {i["name"] for i in inspector.get_indexes("payments")}

        for idx in [
            "ix_payments_gateway_name",
            "ix_payments_track_id",
            "ix_payments_reference_id",
            "ix_payments_invoice_reference",
            "ix_payments_provider_reference",
        ]:
            if idx in indexes:
                op.drop_index(idx, table_name="payments")

        for col in [
            "metadata",
            "paid_at",
            "card_info",
            "country",
            "ip_address",
            "payment_id_gateway",
            "authorization_id",
            "track_id",
            "reference_id",
            "customer_reference",
            "invoice_reference",
            "gateway_name",
            "deposit_status",
            "due_deposit",
            "vat_amount",
            "service_charge",
            "booking_data",
            "customer_data",
        ]:
            if col in cols:
                op.drop_column("payments", col)
