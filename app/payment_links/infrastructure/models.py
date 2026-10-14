"""
app/payment_links/infrastructure/models.py
───────────────────────────────────────────
``PaymentLink``        — one row per issued payment link (request, gateway
                         request/response, webhook result, target, invoice).
                         Creating a link NEVER creates a ``payments`` row; that
                         row is created only after the payment succeeded.
``PaymentWebhookEvent`` — inbox table for gateway webhooks.

``(gateway, event_reference)`` is unique so a webhook retried/duplicated by the
gateway is processed exactly once (idempotent consumer). The payload kept for
audit is sanitised by the caller (no card data).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, Index, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PaymentLinkStatus(str, Enum):
    CREATED = "created"  # row inserted, gateway not called yet
    PENDING = "pending"  # link issued, waiting for the customer
    PAID = "paid"
    FAILED = "failed"  # last attempt failed; the link can still be paid
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class PaymentLinkTarget(str, Enum):
    BOOKING = "booking"
    GIFT_VOUCHER = "gift_voucher"
    PRODUCT = "product"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentLink(Base):
    __tablename__ = "payment_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    link_number: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)

    # what the link is for
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)  # booking/voucher number

    # accounting invoice (ushanr)
    invoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # gateway
    gateway: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_label: Mapped[str] = mapped_column(String(50), nullable=False)
    gateway_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    amount_refunded: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=PaymentLinkStatus.CREATED.value)

    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    customer_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # audit trail
    request_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # our API request body
    gateway_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    gateway_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    webhook_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # last verified payment state
    failure_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # needs_attention etc.

    # result of a successful payment
    gateway_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    track_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_row_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_payment_links_target", "target_type", "target_id"),
        Index("ix_payment_links_gateway_invoice", "gateway", "gateway_invoice_id"),
        Index("ix_payment_links_invoice_number", "invoice_number"),
    )


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gateway: Mapped[str] = mapped_column(String(30), nullable=False)
    event_reference: Mapped[str] = mapped_column(String(150), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # received | processed | ignored | failed
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("gateway", "event_reference", name="uq_payment_webhook_gateway_reference"),
        Index("ix_payment_webhook_events_invoice_id", "invoice_id"),
    )
