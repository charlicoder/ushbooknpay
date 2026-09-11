"""
app/payment/infrastructure/models.py
──────────────────────────────────────
SQLAlchemy ORM models for the Payment domain.

IMPORTANT: Never store raw card numbers, CVV, or full PAN.
Use provider tokenisation and store only provider references.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.payment.domain.value_objects import (
    PaymentFor,
    PaymentTransactionStatus,
)


class Payment(Base):
    """
    Payment record associated with a booking, voucher, product order, or other service.

    Stores transaction, gateway, financial breakdown, participant snapshots,
    and raw gateway data for auditing, accounting, and finance dashboard reporting.

    Required fields: customer_id, total_amount, total_duration, currency.
    All other fields are optional.
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Participants ───────────────────────────────────────────────────────
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    sender_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sender_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── Service & Location ────────────────────────────────────────────────
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_arrangement_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── Service Pricing Breakdown ─────────────────────────────────────────
    addons: Mapped[dict | None] = mapped_column(JSONB, nullable=True)          # list of addon objects
    addons_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True
    )
    extra_time: Mapped[int | None] = mapped_column(Integer, nullable=True)     # extra time in minutes
    price_for_extra_time: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True
    )

    # ── Financial Totals (required) ───────────────────────────────────────
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    total_duration: Mapped[int] = mapped_column(Integer, nullable=False)        # total duration in minutes
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Status ────────────────────────────────────────────────────────────
    status: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        default=PaymentTransactionStatus.INITIATED.value,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Recipient (gift / voucher recipient) ──────────────────────────────
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    recipient_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recipient_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── Associations ──────────────────────────────────────────────────────
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    booking_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    voucher_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    voucher_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    product_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    product_order_items: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # list of ordered items

    # ── Invoice & Transaction Identifiers ─────────────────────────────────
    invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_value: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True
    )
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    track_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transaction_status: Mapped[str | None] = mapped_column(String(50), nullable=True)   # raw gateway status string
    transaction_date: Mapped[str | None] = mapped_column(String(100), nullable=True)    # raw gateway date string
    receipt_image: Mapped[str | None] = mapped_column(Text, nullable=True)              # URL to receipt image

    # ── Payment Classification ────────────────────────────────────────────
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_through: Mapped[str | None] = mapped_column(String(20), nullable=True)     # ushspa | desk | other
    payment_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)    # MyFatoorah | DirectLink | Deema | Other
    payment_gateway: Mapped[str | None] = mapped_column(String(20), nullable=True)     # KNET | TAP | Other
    payment_for: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        default=PaymentFor.BRANCH_SERVICE.value,
    )

    # ── Payment Identifiers & Raw Data ────────────────────────────────────
    payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)         # gateway PaymentId
    payment_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)            # full gateway payload + extras

    # ── Audit ─────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # ── Relationships ─────────────────────────────────────────────────────
    status_history: Mapped[list["PaymentStatusHistory"]] = relationship(
        "PaymentStatusHistory",
        back_populates="payment",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_payments_customer_id", "customer_id"),
        Index("ix_payments_sender_id", "sender_id"),
        Index("ix_payments_service_id", "service_id"),
        Index("ix_payments_branch_id", "branch_id"),
        Index("ix_payments_service_arrangement_id", "service_arrangement_id"),
        Index("ix_payments_booking_id", "booking_id"),
        Index("ix_payments_voucher_id", "voucher_id"),
        Index("ix_payments_product_order_id", "product_order_id"),
        Index("ix_payments_recipient_id", "recipient_id"),
        Index("ix_payments_status", "status"),
        Index("ix_payments_paid_at", "paid_at"),
        Index("ix_payments_payment_for", "payment_for"),
        Index("ix_payments_payment_provider", "payment_provider"),
        Index("ix_payments_payment_through", "payment_through"),
        Index("ix_payments_payment_gateway", "payment_gateway"),
        Index("ix_payments_payment_id", "payment_id"),
        Index("ix_payments_transaction_id", "transaction_id"),
        Index("ix_payments_invoice_id", "invoice_id"),
        Index("ix_payments_track_id", "track_id"),
        Index("ix_payments_reference_id", "reference_id"),
        Index("ix_payments_created_at", "created_at"),
        Index("ix_payments_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id} status={self.status} "
            f"total={self.total_amount} {self.currency} "
            f"for={self.payment_for} booking={self.booking_id}>"
        )


class PaymentStatusHistory(Base):
    """Immutable audit trail for every payment status change."""

    __tablename__ = "payment_status_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    payment: Mapped["Payment"] = relationship("Payment", back_populates="status_history")

    __table_args__ = (
        Index("ix_payment_status_history_payment_id", "payment_id"),
    )
