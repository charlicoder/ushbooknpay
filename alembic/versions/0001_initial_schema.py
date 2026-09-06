"""
0001_initial_schema
────────────────────
Initial schema migration for ushbooknpay.

Creates:
- bookings
- booking_status_history
- temporary_holds
- payments
- payment_status_history
- outbox_events

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-08-21 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── Table: bookings ───────────────────────────────────────────────────
    if "bookings" not in tables:
        op.create_table(
            "bookings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("customer_name", sa.String(200), nullable=True),
            sa.Column("customer_phone", sa.String(30), nullable=True),
            sa.Column("customer_email", sa.String(254), nullable=True),
            sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_arrangement_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("therapist_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("branch_name", sa.String(200), nullable=True),
            sa.Column("service_name", sa.String(200), nullable=True),
            sa.Column("arrangement_name", sa.String(200), nullable=True),
            sa.Column("therapist_name", sa.String(200), nullable=True),
            sa.Column("addons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("appointment_date", sa.DateTime(timezone=True), nullable=False),
            sa.Column("appointment_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("appointment_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("duration_minutes", sa.Integer(), nullable=False),
            sa.Column("service_type", sa.String(20), nullable=False, server_default="branch"),
            sa.Column("home_address_line1", sa.String(255), nullable=True),
            sa.Column("home_address_line2", sa.String(255), nullable=True),
            sa.Column("home_city", sa.String(100), nullable=True),
            sa.Column("home_country", sa.String(2), nullable=True),
            sa.Column("home_latitude", sa.Numeric(precision=10, scale=7), nullable=True),
            sa.Column("home_longitude", sa.Numeric(precision=11, scale=7), nullable=True),
            sa.Column("base_price", sa.Numeric(precision=10, scale=3), nullable=False),
            sa.Column("arrangement_price", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
            sa.Column("addon_price", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
            sa.Column("discount", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
            sa.Column("tax", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
            sa.Column("fees", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"),
            sa.Column("total_amount", sa.Numeric(precision=10, scale=3), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="KWD"),
            sa.Column("status", sa.String(30), nullable=False, server_default="requested"),
            sa.Column("payment_status", sa.String(30), nullable=False, server_default="not_initiated"),
            sa.Column("idempotency_key", sa.String(255), nullable=True),
            sa.Column("customer_notes", sa.Text(), nullable=True),
            sa.Column("internal_notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("idempotency_key", name="uq_bookings_idempotency_key"),
        )
        op.create_index("ix_bookings_customer_id", "bookings", ["customer_id"])
        op.create_index("ix_bookings_branch_date", "bookings", ["branch_id", "appointment_date"])
        op.create_index("ix_bookings_therapist_date", "bookings", ["therapist_id", "appointment_date"])
        op.create_index("ix_bookings_status", "bookings", ["status"])

    # ── Table: booking_status_history ─────────────────────────────────────
    if "booking_status_history" not in tables:
        op.create_table(
            "booking_status_history",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
            sa.Column("old_status", sa.String(30), nullable=True),
            sa.Column("new_status", sa.String(30), nullable=False),
            sa.Column("source", sa.String(100), nullable=True),
            sa.Column("changed_by", sa.String(255), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("correlation_id", sa.String(255), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )
        op.create_index("ix_booking_status_history_booking_id", "booking_status_history", ["booking_id"])

    # ── Table: temporary_holds ────────────────────────────────────────────
    if "temporary_holds" not in tables:
        op.create_table(
            "temporary_holds",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("therapist_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("service_arrangement_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("appointment_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("appointment_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )
        op.create_index("ix_temporary_holds_expires_at", "temporary_holds", ["expires_at"])
        op.create_index("ix_temporary_holds_therapist", "temporary_holds", ["therapist_id", "appointment_start"])

    # ── Table: payments ───────────────────────────────────────────────────
    if "payments" not in tables:
        op.create_table(
            "payments",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("booking_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
            sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("amount", sa.Numeric(precision=10, scale=3), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="KWD"),
            sa.Column("provider", sa.String(30), nullable=False),
            sa.Column("provider_payment_id", sa.String(255), nullable=True),
            sa.Column("provider_reference", sa.String(255), nullable=True),
            sa.Column("provider_transaction_id", sa.String(255), nullable=True),
            sa.Column("payment_method", sa.String(30), nullable=False, server_default="unknown"),
            sa.Column("status", sa.String(30), nullable=False, server_default="initiated"),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("payment_url", sa.Text(), nullable=True),
            sa.Column("provider_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("idempotency_key", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        )
        op.create_index("ix_payments_booking_id", "payments", ["booking_id"])
        op.create_index("ix_payments_customer_id", "payments", ["customer_id"])
        op.create_index("ix_payments_provider_payment_id", "payments", ["provider_payment_id"])
        op.create_index("ix_payments_status", "payments", ["status"])

    # ── Table: payment_status_history ─────────────────────────────────────
    if "payment_status_history" not in tables:
        op.create_table(
            "payment_status_history",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("old_status", sa.String(30), nullable=True),
            sa.Column("new_status", sa.String(30), nullable=False),
            sa.Column("source", sa.String(100), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("provider_reference", sa.String(255), nullable=True),
            sa.Column("correlation_id", sa.String(255), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )
        op.create_index("ix_payment_status_history_payment_id", "payment_status_history", ["payment_id"])

    # ── Table: outbox_events ──────────────────────────────────────────────
    if "outbox_events" not in tables:
        op.create_table(
            "outbox_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("event_name", sa.String(100), nullable=False),
            sa.Column("queue_url", sa.String(512), nullable=False),
            sa.Column("message_group_id", sa.String(128), nullable=True),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("correlation_id", sa.String(255), nullable=True),
        )
        op.create_index("ix_outbox_events_status_scheduled", "outbox_events", ["status", "scheduled_at"])
        op.create_index("ix_outbox_events_event_name", "outbox_events", ["event_name"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "outbox_events" in tables:
        op.drop_table("outbox_events")
    if "payment_status_history" in tables:
        op.drop_table("payment_status_history")
    if "payments" in tables:
        op.drop_table("payments")
    if "temporary_holds" in tables:
        op.drop_table("temporary_holds")
    if "booking_status_history" in tables:
        op.drop_table("booking_status_history")
    if "bookings" in tables:
        op.drop_table("bookings")
