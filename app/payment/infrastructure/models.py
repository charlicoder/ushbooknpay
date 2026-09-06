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

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.payment.domain.value_objects import (
    PaymentFor,
    PaymentMethod,
    PaymentProvider,
    PaymentTransactionStatus,
)


class Payment(Base):
    """
    Payment record associated with a booking, voucher, or other service.

    Stores comprehensive transaction, gateway, financial breakdown,
    booking snapshot, customer snapshot, and voucher data for auditing,
    accounting, and finance dashboard reporting.
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Association & Purpose ─────────────────────────────────────────────
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    voucher_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    voucher_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, server_default=text("'{}'::jsonb")
    )
    payment_for: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=PaymentFor.SERVICE.value,
        server_default=text("'service'"),
    )

    # ── Standard Unified Payment / Gateway Fields ─────────────────────────
    payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Gateway PaymentId
    transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Gateway TransactionId
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Gateway InvoiceId
    invoice_value: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True
    )
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_mobile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_date: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transaction_date: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_gateway: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Snapshots for Auditing & Accounting ────────────────────────────────
    customer_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, server_default=text("'{}'::jsonb")
    )
    booking_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, server_default=text("'{}'::jsonb")
    )

    # ── Financial Breakdown (KWD 3 Decimals) ──────────────────────────────
    amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")
    service_charge: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True, default=Decimal("0.000")
    )
    vat_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True, default=Decimal("0.000")
    )
    due_deposit: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True
    )
    deposit_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default="Not Deposited"
    )

    # ── Provider & Gateway Identifiers ────────────────────────────────────
    provider: Mapped[str] = mapped_column(String(30), nullable=False)  # e.g. myfatoorah, tap, knet, cash
    gateway_name: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. KNET, VISA/MASTER, APPLE_PAY
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. InvoiceId "7103271"
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. InvoiceReference "2026193793"
    provider_transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. TransactionId "623710001297726"
    invoice_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Bank/KNET ReferenceId "623710000033"
    track_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Gateway TrackId "25-08-2026_3759584"
    authorization_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Auth code "B61005"
    payment_id_gateway: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Gateway PaymentId "100623710000000606"

    # ── Method & Status ───────────────────────────────────────────────────
    payment_method: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PaymentMethod.UNKNOWN.value
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=PaymentTransactionStatus.INITIATED.value,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── URLs & Timestamps ─────────────────────────────────────────────────
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Network, Card & Metadata ──────────────────────────────────────────
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    card_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    # ── Raw Provider Response (Full payload for audit/debugging) ──────────
    provider_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── Idempotency ───────────────────────────────────────────────────────
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Audit Timestamps ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    status_history: Mapped[list["PaymentStatusHistory"]] = relationship(
        "PaymentStatusHistory",
        back_populates="payment",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_payments_booking_id", "booking_id"),
        Index("ix_payments_customer_id", "customer_id"),
        Index("ix_payments_voucher_id", "voucher_id"),
        Index("ix_payments_payment_for", "payment_for"),
        Index("ix_payments_payment_id", "payment_id"),
        Index("ix_payments_transaction_id", "transaction_id"),
        Index("ix_payments_is_paid", "is_paid"),
        Index("ix_payments_invoice_id", "invoice_id"),
        Index("ix_payments_payment_gateway", "payment_gateway"),
        Index("ix_payments_provider_payment_id", "provider_payment_id"),
        Index("ix_payments_provider_reference", "provider_reference"),
        Index("ix_payments_invoice_reference", "invoice_reference"),
        Index("ix_payments_customer_reference", "customer_reference"),
        Index("ix_payments_reference_id", "reference_id"),
        Index("ix_payments_track_id", "track_id"),
        Index("ix_payments_gateway_name", "gateway_name"),
        Index("ix_payments_status", "status"),
        Index("ix_payments_created_at", "created_at"),
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
    )

    def __init__(self, **kwargs: Any) -> None:
        if "payment_for" not in kwargs or kwargs["payment_for"] is None:
            kwargs["payment_for"] = PaymentFor.SERVICE.value
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<Payment id={self.id} status={self.status} amount={self.amount} for={self.payment_for} booking={self.booking_id}>"


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
