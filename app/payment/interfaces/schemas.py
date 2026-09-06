"""
app/payment/interfaces/schemas.py
───────────────────────────────────
Pydantic v2 schemas for the Payment API.

Supports complete CRUD operations, detailed financial audit tracking,
gateway response ingestion, and reporting analytics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.payment.domain.value_objects import (
    PaymentFor,
    PaymentMethod,
    PaymentProvider,
    PaymentTransactionStatus,
)


# ── Request Schemas ───────────────────────────────────────────────────────────


class InitiatePaymentRequest(BaseModel):
    """POST /api/v1/payments/initiate/"""

    booking_id: str | None = Field(default=None, description="Booking ID if paying for a booking.")
    voucher_id: str | None = Field(default=None, description="Voucher ID if paying for a voucher.")
    voucher_data: dict[str, Any] | None = Field(default=None, description="Voucher snapshot data.")
    payment_for: PaymentFor = Field(
        default=PaymentFor.SERVICE,
        description="Purpose: gift_voucher, service, home_service, products, loyalty, others",
    )
    provider: PaymentProvider = PaymentProvider.MYFATOORAH
    payment_method: str = "card"  # card | knet | apple_pay | google_pay


class CreatePaymentRequestSchema(BaseModel):
    """POST /api/v1/payments/ (Create payment record / ingest gateway response)."""

    booking_id: uuid.UUID | str | None = Field(
        default=None,
        description="Associated booking ID. If omitted, can be inferred from gateway response CustomerReference.",
    )
    voucher_id: uuid.UUID | str | None = Field(
        default=None,
        description="Associated gift voucher ID, if payment is for a voucher.",
    )
    voucher_data: dict[str, Any] | None = Field(
        default=None,
        description="Snapshot of voucher metadata at the time of payment.",
    )
    payment_for: str | None = Field(
        default=None,
        description="Payment purpose: gift_voucher, service, home_service, products, loyalty, others.",
    )
    customer_id: uuid.UUID | str | None = Field(
        default=None,
        description="Associated customer ID. Inferred from booking or current user if omitted.",
    )
    amount: Decimal | str | float | None = Field(
        default=None,
        description="Payment amount. If omitted, parsed from gateway response.",
    )
    currency: str = Field(default="KWD", description="Currency code (KWD, KD, etc.).")
    provider: str = Field(default="myfatoorah", description="Gateway provider name.")
    payment_method: str = Field(default="card", description="card, knet, apple_pay, etc.")
    status: str = Field(
        default=PaymentTransactionStatus.SUCCESS.value,
        description="Transaction status: initiated, pending, success, failed, cancelled, refunded.",
    )

    # Raw full gateway response payload
    gateway_response: dict[str, Any] | None = Field(
        default=None,
        description="Full raw response data from the payment gateway (e.g. MyFatoorah, Tap).",
    )

    # ── Standard Unified Payment / Gateway Fields ─────────────────────────
    payment_id: str | None = Field(default=None, description="Gateway PaymentId")
    transaction_id: str | None = Field(default=None, description="Gateway TransactionId")
    is_paid: bool | None = Field(default=None, description="True if payment is captured/paid")
    invoice_id: str | None = Field(default=None, description="Gateway InvoiceId")
    invoice_value: Decimal | str | float | None = Field(default=None, description="Invoice value")
    customer_name: str | None = Field(default=None, description="Customer name")
    customer_mobile: str | None = Field(default=None, description="Customer mobile number")
    customer_email: str | None = Field(default=None, description="Customer email address")
    created_date: str | None = Field(default=None, description="Gateway CreatedDate string")
    transaction_date: str | None = Field(default=None, description="Gateway TransactionDate string")
    payment_gateway: str | None = Field(default=None, description="Gateway name (KNET, VISA/MASTER, etc.)")

    # Explicit financial fields
    service_charge: Decimal | str | float | None = Field(default=None)
    vat_amount: Decimal | str | float | None = Field(default=None)
    due_deposit: Decimal | str | float | None = Field(default=None)
    deposit_status: str | None = Field(default=None)

    # Gateway identifiers
    invoice_reference: str | None = None
    customer_reference: str | None = None
    reference_id: str | None = None
    track_id: str | None = None
    authorization_id: str | None = None
    gateway_name: str | None = None
    provider_payment_id: str | None = None
    provider_transaction_id: str | None = None

    # Customer and booking metadata overrides
    customer_data: dict[str, Any] | None = None
    booking_data: dict[str, Any] | None = None
    card_info: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    failure_reason: str | None = None
    idempotency_key: str | None = None

    model_config = {"extra": "allow"}


class UpdatePaymentRequestSchema(BaseModel):
    """PATCH /api/v1/payments/{payment_id}/ (Partial/full update)."""

    booking_id: uuid.UUID | str | None = None
    voucher_id: uuid.UUID | str | None = None
    voucher_data: dict[str, Any] | None = None
    payment_for: str | None = None
    status: str | None = Field(default=None, description="New payment status")
    is_paid: bool | None = Field(default=None, description="True if payment is captured/paid")
    payment_id: str | None = Field(default=None, description="Gateway PaymentId")
    transaction_id: str | None = Field(default=None, description="Gateway TransactionId")
    invoice_id: str | None = Field(default=None, description="Gateway InvoiceId")
    invoice_value: Decimal | str | float | None = Field(default=None)
    customer_name: str | None = Field(default=None)
    customer_mobile: str | None = Field(default=None)
    customer_email: str | None = Field(default=None)
    created_date: str | None = Field(default=None)
    transaction_date: str | None = Field(default=None)
    payment_gateway: str | None = Field(default=None)
    invoice_reference: str | None = None
    customer_reference: str | None = None
    failure_reason: str | None = Field(default=None, description="Reason if payment failed")
    deposit_status: str | None = Field(default=None, description="Deposit status: Deposited, Not Deposited")
    due_deposit: Decimal | str | float | None = Field(default=None)
    service_charge: Decimal | str | float | None = Field(default=None)
    vat_amount: Decimal | str | float | None = Field(default=None)
    gateway_response: dict[str, Any] | None = Field(default=None)
    customer_data: dict[str, Any] | None = None
    booking_data: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    reason: str | None = Field(default=None, description="Reason for update audit trail")
    source: str = Field(default="admin", description="Source/actor making the change")


# ── Response Schemas ──────────────────────────────────────────────────────────


class PaymentStatusHistoryItem(BaseModel):
    """Audit log item for payment status changes."""

    id: str
    old_status: str | None = None
    new_status: str
    source: str | None = None
    reason: str | None = None
    provider_reference: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime


class PaymentListItem(BaseModel):
    """Payment record summary for lists and dashboards."""

    id: str
    booking_id: str | None = None
    customer_id: str
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    payment_for: str = "service"
    amount: str
    currency: str
    provider: str
    payment_method: str
    status: str
    is_paid: bool = False
    payment_id: str | None = None
    transaction_id: str | None = None
    invoice_id: str | None = None
    invoice_value: str | None = None
    invoice_reference: str | None = None
    customer_reference: str | None = None
    customer_name: str | None = None
    customer_mobile: str | None = None
    customer_email: str | None = None
    created_date: str | None = None
    transaction_date: str | None = None
    payment_gateway: str | None = None
    gateway_name: str | None = None
    reference_id: str | None = None
    track_id: str | None = None
    service_charge: str | None = None
    vat_amount: str | None = None
    due_deposit: str | None = None
    deposit_status: str | None = None
    payment_url: str | None = None
    customer_data: dict[str, Any] | None = None
    booking_data: dict[str, Any] | None = None
    paid_at: datetime | None = None
    created_at: datetime


class PaymentDetailResponse(BaseModel):
    """Full detail of a payment record for finance dashboards and audits."""

    id: str
    booking_id: str | None = None
    customer_id: str
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    payment_for: str = "service"
    amount: str
    currency: str
    service_charge: str = "0.000"
    vat_amount: str = "0.000"
    due_deposit: str | None = None
    deposit_status: str | None = "Not Deposited"
    provider: str
    gateway_name: str | None = None
    payment_gateway: str | None = None
    payment_method: str
    status: str
    is_paid: bool = False
    payment_id: str | None = None
    transaction_id: str | None = None
    invoice_id: str | None = None
    invoice_value: str | None = None
    invoice_reference: str | None = None
    customer_reference: str | None = None
    customer_name: str | None = None
    customer_mobile: str | None = None
    customer_email: str | None = None
    created_date: str | None = None
    transaction_date: str | None = None
    provider_payment_id: str | None = None
    provider_reference: str | None = None
    provider_transaction_id: str | None = None
    reference_id: str | None = None
    track_id: str | None = None
    authorization_id: str | None = None
    payment_id_gateway: str | None = None
    payment_url: str | None = None
    failure_reason: str | None = None
    ip_address: str | None = None
    country: str | None = None
    paid_at: datetime | None = None
    customer_data: dict[str, Any] | None = None
    booking_data: dict[str, Any] | None = None
    card_info: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    provider_response: dict[str, Any] | None = None
    status_history: list[PaymentStatusHistoryItem] | None = None
    created_at: datetime
    updated_at: datetime


class PaymentResponse(BaseModel):
    """Standard single payment response envelope."""

    success: bool = True
    data: PaymentDetailResponse
    message: str | None = None


class PaymentListResponse(BaseModel):
    """Paginated list of payments response."""

    success: bool = True
    data: list[PaymentListItem]
    meta: dict[str, Any]


class PaymentSessionResponse(BaseModel):
    """Returned when a payment session is created."""

    payment_id: str
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    provider: str
    payment_url: str
    amount: str
    currency: str
    expires_at: datetime | None = None


class PaymentStatusResponse(BaseModel):
    """Payment status for a booking or voucher."""

    payment_id: str
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    provider: str
    status: str
    amount: str
    currency: str
    payment_method: str
    provider_reference: str | None
    created_at: datetime
    updated_at: datetime


class WebhookVerifyRequest(BaseModel):
    """Internal-use: triggered by webhook to verify and update payment."""

    provider: str
    provider_payment_id: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
