"""add payments model overhaul

Revision ID: 0025_payments_overhaul
Revises: 0024_voucher_redeemed_by
Create Date: 2026-09-10

Redesigns the `payments` table to match the canonical field specification:

ADD new columns:
  sender_id, sender_data, service_id, service_data, branch_id, branch_data,
  service_arrangement_id, service_arrangement_data, addons, addons_price,
  extra_time, price_for_extra_time, total_amount (copy of amount), total_duration,
  recipient_id, recipient_phone, recipient_data, product_order_id, product_order_items,
  transaction_status, receipt_image, payment_through, payment_provider (new column, copy of provider),
  payment_data, created_by

DATA MIGRATION:
  - Copies amount → total_amount, provider → payment_provider
  - Migrates payment_for values to new enum names (service→branch_service, products→product_items, etc.)

DROP old columns (after data migration):
  amount, is_paid, customer_name, customer_mobile, customer_email, created_date,
  service_charge, vat_amount, due_deposit, deposit_status, provider, gateway_name,
  provider_payment_id, provider_reference, provider_transaction_id, invoice_reference,
  customer_reference, authorization_id, payment_id_gateway, failure_reason,
  ip_address, card_info, metadata, provider_response, idempotency_key, updated_at

RENAME:
  gateway_name → payment_gateway (add new col, copy, drop old)

All additions guarded with IF NOT EXISTS, all drops guarded with IF EXISTS.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_payments_overhaul"
down_revision: Union[str, None] = "0024_voucher_redeemed_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tables = inspector.get_table_names()
    if "payments" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("payments")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("payments")}

    # ── 1. ADD new columns ────────────────────────────────────────────────

    new_columns = [
        ("sender_id", sa.Column("sender_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("sender_data", sa.Column("sender_data", postgresql.JSONB(), nullable=True)),
        ("service_id", sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("service_data", sa.Column("service_data", postgresql.JSONB(), nullable=True)),
        ("branch_id", sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("branch_data", sa.Column("branch_data", postgresql.JSONB(), nullable=True)),
        ("service_arrangement_id", sa.Column("service_arrangement_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("service_arrangement_data", sa.Column("service_arrangement_data", postgresql.JSONB(), nullable=True)),
        ("addons", sa.Column("addons", postgresql.JSONB(), nullable=True)),
        ("addons_price", sa.Column("addons_price", sa.Numeric(precision=10, scale=3), nullable=True)),
        ("extra_time", sa.Column("extra_time", sa.Integer(), nullable=True)),
        ("price_for_extra_time", sa.Column("price_for_extra_time", sa.Numeric(precision=10, scale=3), nullable=True)),
        ("total_amount", sa.Column("total_amount", sa.Numeric(precision=10, scale=3), nullable=True)),
        ("total_duration", sa.Column("total_duration", sa.Integer(), nullable=True)),
        ("recipient_id", sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("recipient_phone", sa.Column("recipient_phone", sa.String(50), nullable=True)),
        ("recipient_data", sa.Column("recipient_data", postgresql.JSONB(), nullable=True)),
        ("product_order_id", sa.Column("product_order_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("product_order_items", sa.Column("product_order_items", postgresql.JSONB(), nullable=True)),
        ("transaction_status", sa.Column("transaction_status", sa.String(50), nullable=True)),
        ("receipt_image", sa.Column("receipt_image", sa.Text(), nullable=True)),
        ("payment_through", sa.Column("payment_through", sa.String(20), nullable=True)),
        ("payment_provider", sa.Column("payment_provider", sa.String(20), nullable=True)),
        ("payment_gateway", sa.Column("payment_gateway", sa.String(20), nullable=True)),
        ("payment_data", sa.Column("payment_data", postgresql.JSONB(), nullable=True)),
        ("created_by", sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True)),
    ]

    for col_name, col_def in new_columns:
        if col_name not in existing_cols:
            op.add_column("payments", col_def)

    # ── 2. DATA MIGRATION ─────────────────────────────────────────────────

    # Copy amount → total_amount (only where total_amount is null)
    if "amount" in existing_cols:
        op.execute(
            "UPDATE payments SET total_amount = amount WHERE total_amount IS NULL"
        )

    # Set total_duration default where null
    op.execute(
        "UPDATE payments SET total_duration = 0 WHERE total_duration IS NULL"
    )

    # Copy provider → payment_provider (normalise to new enum values)
    if "provider" in existing_cols:
        op.execute("""
            UPDATE payments SET payment_provider = CASE
                WHEN LOWER(provider) IN ('myfatoorah', 'fatoorah', 'myfatora') THEN 'MyFatoorah'
                WHEN LOWER(provider) IN ('directlink', 'direct_link', 'direct') THEN 'DirectLink'
                WHEN LOWER(provider) IN ('deema') THEN 'Deema'
                WHEN LOWER(provider) IN ('tap') THEN 'Other'
                ELSE 'Other'
            END
            WHERE payment_provider IS NULL AND provider IS NOT NULL
        """)

    # Copy gateway_name → payment_gateway (normalise to spec values)
    if "gateway_name" in existing_cols:
        op.execute("""
            UPDATE payments SET payment_gateway = CASE
                WHEN UPPER(gateway_name) LIKE '%KNET%' OR UPPER(gateway_name) LIKE '%K-NET%' THEN 'KNET'
                WHEN UPPER(gateway_name) LIKE '%TAP%' THEN 'TAP'
                WHEN gateway_name IS NOT NULL AND gateway_name != '' THEN 'Other'
                ELSE NULL
            END
            WHERE payment_gateway IS NULL AND gateway_name IS NOT NULL
        """)

    # Migrate payment_for enum values
    op.execute("""
        UPDATE payments SET payment_for = CASE
            WHEN payment_for IN ('service', 'branch_service') THEN 'branch_service'
            WHEN payment_for IN ('home_service', 'home') THEN 'home_service'
            WHEN payment_for IN ('gift_voucher', 'voucher') THEN 'gift_voucher'
            WHEN payment_for IN ('products', 'product', 'product_items') THEN 'product_items'
            WHEN payment_for IN ('loyalty', 'others', 'other') THEN 'branch_service'
            ELSE 'branch_service'
        END
        WHERE payment_for IS NOT NULL
    """)

    # Set default payment_for for nulls
    op.execute(
        "UPDATE payments SET payment_for = 'branch_service' WHERE payment_for IS NULL"
    )

    # Now make total_amount and total_duration NOT NULL (they have values)
    op.execute("ALTER TABLE payments ALTER COLUMN total_amount SET NOT NULL")
    op.execute("ALTER TABLE payments ALTER COLUMN total_duration SET NOT NULL")

    # ── 3. ADD new indexes ────────────────────────────────────────────────

    new_indexes = [
        ("ix_payments_sender_id", ["sender_id"]),
        ("ix_payments_service_id", ["service_id"]),
        ("ix_payments_branch_id", ["branch_id"]),
        ("ix_payments_service_arrangement_id", ["service_arrangement_id"]),
        ("ix_payments_recipient_id", ["recipient_id"]),
        ("ix_payments_product_order_id", ["product_order_id"]),
        ("ix_payments_payment_provider", ["payment_provider"]),
        ("ix_payments_payment_through", ["payment_through"]),
        ("ix_payments_payment_gateway", ["payment_gateway"]),
        ("ix_payments_paid_at", ["paid_at"]),
        ("ix_payments_created_by", ["created_by"]),
    ]

    for idx_name, idx_cols in new_indexes:
        if idx_name not in existing_indexes:
            op.create_index(idx_name, "payments", idx_cols)

    # ── 4. DROP old columns ───────────────────────────────────────────────

    # Drop old indexes that reference columns being dropped
    old_indexes_to_drop = [
        ("ix_payments_is_paid", "is_paid"),
        ("ix_payments_provider_payment_id", "provider_payment_id"),
        ("ix_payments_provider_reference", "provider_reference"),
        ("ix_payments_invoice_reference", "invoice_reference"),
        ("ix_payments_customer_reference", "customer_reference"),
        ("ix_payments_gateway_name", "gateway_name"),
    ]
    # Also drop unique constraint on idempotency_key
    try:
        op.drop_constraint("uq_payments_idempotency_key", "payments", type_="unique")
    except Exception:
        pass

    for idx_name, col_name in old_indexes_to_drop:
        if idx_name in existing_indexes:
            op.drop_index(idx_name, table_name="payments")

    # Drop old columns
    columns_to_drop = [
        "amount",
        "is_paid",
        "customer_name",
        "customer_mobile",
        "customer_email",
        "created_date",
        "service_charge",
        "vat_amount",
        "due_deposit",
        "deposit_status",
        "provider",
        "gateway_name",
        "provider_payment_id",
        "provider_reference",
        "provider_transaction_id",
        "invoice_reference",
        "customer_reference",
        "authorization_id",
        "payment_id_gateway",
        "failure_reason",
        "ip_address",
        "card_info",
        "metadata",
        "provider_response",
        "idempotency_key",
        "updated_at",
    ]

    # Re-fetch existing cols after adds
    existing_cols_after = {c["name"] for c in inspector.get_columns("payments")}
    for col in columns_to_drop:
        if col in existing_cols_after:
            op.drop_column("payments", col)


def downgrade() -> None:
    """
    Downgrade is intentionally limited: restores dropped columns with NULL values
    (original data cannot be recovered after destructive migration).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "payments" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("payments")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("payments")}

    # Drop new indexes
    new_idx_to_drop = [
        "ix_payments_sender_id",
        "ix_payments_service_id",
        "ix_payments_branch_id",
        "ix_payments_service_arrangement_id",
        "ix_payments_recipient_id",
        "ix_payments_product_order_id",
        "ix_payments_payment_provider",
        "ix_payments_payment_through",
        "ix_payments_payment_gateway",
        "ix_payments_paid_at",
        "ix_payments_created_by",
    ]
    for idx in new_idx_to_drop:
        if idx in existing_indexes:
            op.drop_index(idx, table_name="payments")

    # Drop new columns
    new_cols_to_drop = [
        "sender_id", "sender_data", "service_id", "service_data", "branch_id", "branch_data",
        "service_arrangement_id", "service_arrangement_data", "addons", "addons_price",
        "extra_time", "price_for_extra_time", "total_duration",
        "recipient_id", "recipient_phone", "recipient_data", "product_order_id",
        "product_order_items", "transaction_status", "receipt_image",
        "payment_through", "payment_provider", "payment_gateway", "payment_data", "created_by",
    ]
    for col in new_cols_to_drop:
        if col in existing_cols:
            op.drop_column("payments", col)

    # Restore amount column from total_amount
    if "amount" not in existing_cols:
        op.add_column("payments", sa.Column("amount", sa.Numeric(precision=10, scale=3), nullable=True))
        op.execute("UPDATE payments SET amount = total_amount")
        op.execute("ALTER TABLE payments ALTER COLUMN amount SET NOT NULL")

    # Drop total_amount
    if "total_amount" in existing_cols:
        op.drop_column("payments", "total_amount")

    # Restore provider column
    if "provider" not in existing_cols:
        op.add_column("payments", sa.Column("provider", sa.String(30), nullable=True, server_default="myfatoorah"))

    # Restore gateway_name
    if "gateway_name" not in existing_cols:
        op.add_column("payments", sa.Column("gateway_name", sa.String(50), nullable=True))

    # Restore other dropped columns as nullable
    restore_cols = [
        ("is_paid", sa.Column("is_paid", sa.Boolean(), nullable=True, server_default=sa.text("false"))),
        ("customer_name", sa.Column("customer_name", sa.String(255), nullable=True)),
        ("customer_mobile", sa.Column("customer_mobile", sa.String(50), nullable=True)),
        ("customer_email", sa.Column("customer_email", sa.String(255), nullable=True)),
        ("created_date", sa.Column("created_date", sa.String(100), nullable=True)),
        ("service_charge", sa.Column("service_charge", sa.Numeric(10, 3), nullable=True)),
        ("vat_amount", sa.Column("vat_amount", sa.Numeric(10, 3), nullable=True)),
        ("due_deposit", sa.Column("due_deposit", sa.Numeric(10, 3), nullable=True)),
        ("deposit_status", sa.Column("deposit_status", sa.String(50), nullable=True)),
        ("updated_at", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    existing_cols_now = {c["name"] for c in inspector.get_columns("payments")}
    for col_name, col_def in restore_cols:
        if col_name not in existing_cols_now:
            op.add_column("payments", col_def)
