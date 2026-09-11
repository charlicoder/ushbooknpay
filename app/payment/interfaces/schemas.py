"""
app/payment/interfaces/schemas.py
───────────────────────────────────
Pydantic v2 schemas for the Payment API.

Required fields for creation: customer_id, total_amount, total_duration, currency.
All other fields are optional.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.payment.domain.value_objects import (
    PaymentFor,
    PaymentGateway,
    PaymentMethod,
    PaymentProvider,
    PaymentThrough,
    PaymentTransactionStatus,
)


def _coerce_empty(v: Any) -> Any:
    """Coerce empty/whitespace strings to None."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


# ── Request Schemas ───────────────────────────────────────────────────────────


class CreatePaymentRequestSchema(BaseModel):
    """
    POST /api/v1/payments/

    Create a payment record or ingest a gateway response.

    Required: customer_id, total_amount, total_duration, currency.
    All other fields are optional.
    """

    # ── Required ──────────────────────────────────────────────────────────
    customer_id: uuid.UUID = Field(description="Customer UUID (required).")
    total_amount: Decimal = Field(description="Total payment amount (required).")
    total_duration: int = Field(description="Total service duration in minutes (required).")
    currency: str = Field(default="KWD", description="ISO currency code, e.g. KWD.")

    # ── Participants ──────────────────────────────────────────────────────
    customer_data: dict[str, Any] | None = Field(default=None, description="Customer snapshot (JSONB).")
    sender_id: uuid.UUID | None = Field(default=None, description="Sender UUID (gift/voucher sender).")
    sender_data: dict[str, Any] | None = Field(default=None, description="Sender snapshot (JSONB).")

    # ── Service & Location ────────────────────────────────────────────────
    service_id: uuid.UUID | None = Field(default=None, description="Service UUID.")
    service_data: dict[str, Any] | None = Field(default=None, description="Service snapshot (JSONB).")
    branch_id: uuid.UUID | None = Field(default=None, description="Branch UUID.")
    branch_data: dict[str, Any] | None = Field(default=None, description="Branch snapshot (JSONB).")
    service_arrangement_id: uuid.UUID | None = Field(default=None, description="Service arrangement UUID.")
    service_arrangement_data: dict[str, Any] | None = Field(default=None, description="Arrangement snapshot (JSONB).")

    # ── Pricing Breakdown ─────────────────────────────────────────────────
    addons: list[Any] | dict[str, Any] | None = Field(default=None, description="Addon items list (JSONB).")
    addons_price: Decimal | None = Field(default=None, description="Total price of addons.")
    extra_time: int | None = Field(default=None, description="Extra time in minutes.")
    price_for_extra_time: Decimal | None = Field(default=None, description="Price charged for extra time.")

    # ── Geography ─────────────────────────────────────────────────────────
    country: str | None = Field(default=None, description="Country name or ISO code.")

    # ── Status ────────────────────────────────────────────────────────────
    status: str | None = Field(
        default=PaymentTransactionStatus.INITIATED.value,
        description="Payment status: initiated, pending, success, failed, cancelled, refunded.",
    )
    paid_at: datetime | None = Field(default=None, description="Timestamp when payment was settled.")

    # ── Recipient ─────────────────────────────────────────────────────────
    recipient_id: uuid.UUID | None = Field(default=None, description="Recipient UUID.")
    recipient_phone: str | None = Field(default=None, description="Recipient phone number.")
    recipient_data: dict[str, Any] | None = Field(default=None, description="Recipient snapshot (JSONB).")

    # ── Associations ──────────────────────────────────────────────────────
    booking_id: uuid.UUID | None = Field(default=None, description="Associated booking UUID.")
    booking_data: dict[str, Any] | None = Field(default=None, description="Booking snapshot (JSONB).")
    voucher_id: uuid.UUID | None = Field(default=None, description="Associated gift voucher UUID.")
    voucher_data: dict[str, Any] | None = Field(default=None, description="Voucher snapshot (JSONB).")
    product_order_id: uuid.UUID | None = Field(default=None, description="Associated product order UUID.")
    product_order_items: list[Any] | dict[str, Any] | None = Field(default=None, description="Product order items (JSONB).")

    # ── Invoice & Transaction ─────────────────────────────────────────────
    invoice_id: str | None = Field(default=None, description="Gateway invoice ID.")
    invoice_value: Decimal | None = Field(default=None, description="Invoice amount from gateway.")
    payment_url: str | None = Field(default=None, description="Payment URL from gateway.")
    transaction_id: str | None = Field(default=None, description="Gateway transaction ID.")
    track_id: str | None = Field(default=None, description="Gateway track ID.")
    reference_id: str | None = Field(default=None, description="Bank/KNET reference ID.")
    transaction_status: str | None = Field(default=None, description="Raw transaction status from gateway.")
    transaction_date: str | None = Field(default=None, description="Raw transaction date from gateway.")
    receipt_image: str | None = Field(default=None, description="URL to receipt image.")

    # ── Classification ────────────────────────────────────────────────────
    payment_method: str | None = Field(default=None, description="Payment method: card, knet, apple_pay, etc.")
    payment_through: str | None = Field(
        default=None,
        description="Payment channel: ushspa, desk, other.",
    )
    payment_provider: str | None = Field(
        default=None,
        description="Payment provider: MyFatoorah, DirectLink, Deema, Other.",
    )
    payment_gateway: str | None = Field(
        default=None,
        description="Payment gateway/network: KNET, TAP, Other.",
    )
    payment_for: str | None = Field(
        default=PaymentFor.BRANCH_SERVICE.value,
        description="Payment purpose: branch_service, home_service, gift_voucher, product_items.",
    )

    # ── Raw Gateway Data ──────────────────────────────────────────────────
    payment_id: str | None = Field(default=None, description="Gateway payment ID.")
    payment_data: dict[str, Any] | None = Field(default=None, description="Full gateway payload / extra data (JSONB).")

    # ── Ingest helper (full gateway response) ─────────────────────────────
    gateway_response: dict[str, Any] | None = Field(
        default=None,
        description="Full raw response from gateway — will be parsed and merged into payment_data.",
    )

    # ── created_by override (normally auto-set from JWT) ──────────────────
    created_by: uuid.UUID | None = Field(default=None, description="API requester UUID (auto-set from JWT if omitted).")

    # ── Validators ────────────────────────────────────────────────────────
    @field_validator(
        "recipient_phone", "transaction_date", "transaction_status", "receipt_image",
        "country", "invoice_id", "payment_url", "transaction_id", "track_id", "reference_id",
        "payment_method", "payment_through", "payment_provider", "payment_gateway",
        "payment_for", "payment_id", "status",
        mode="before",
    )
    @classmethod
    def coerce_empty_str(cls, v: Any) -> Any:
        return _coerce_empty(v)

    model_config = {"extra": "allow"}


class UpdatePaymentRequestSchema(BaseModel):
    """PATCH /api/v1/payments/{payment_id}/ — All fields optional."""

    # Participants
    customer_data: dict[str, Any] | None = None
    sender_id: uuid.UUID | None = None
    sender_data: dict[str, Any] | None = None

    # Service
    service_id: uuid.UUID | None = None
    service_data: dict[str, Any] | None = None
    branch_id: uuid.UUID | None = None
    branch_data: dict[str, Any] | None = None
    service_arrangement_id: uuid.UUID | None = None
    service_arrangement_data: dict[str, Any] | None = None

    # Pricing
    addons: list[Any] | dict[str, Any] | None = None
    addons_price: Decimal | None = None
    extra_time: int | None = None
    price_for_extra_time: Decimal | None = None

    # Financials
    total_amount: Decimal | None = None
    total_duration: int | None = None
    currency: str | None = None
    country: str | None = None

    # Status
    status: str | None = None
    paid_at: datetime | None = None

    # Recipient
    recipient_id: uuid.UUID | None = None
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] | None = None

    # Associations
    booking_id: uuid.UUID | None = None
    booking_data: dict[str, Any] | None = None
    voucher_id: uuid.UUID | None = None
    voucher_data: dict[str, Any] | None = None
    product_order_id: uuid.UUID | None = None
    product_order_items: list[Any] | dict[str, Any] | None = None

    # Invoice & Transaction
    invoice_id: str | None = None
    invoice_value: Decimal | None = None
    payment_url: str | None = None
    transaction_id: str | None = None
    track_id: str | None = None
    reference_id: str | None = None
    transaction_status: str | None = None
    transaction_date: str | None = None
    receipt_image: str | None = None

    # Classification
    payment_method: str | None = None
    payment_through: str | None = None
    payment_provider: str | None = None
    payment_gateway: str | None = None
    payment_for: str | None = None

    # Payment data
    payment_id: str | None = None
    payment_data: dict[str, Any] | None = None

    # Gateway response ingest
    gateway_response: dict[str, Any] | None = None

    # Audit
    reason: str | None = Field(default=None, description="Reason for update (audit log).")
    source: str = Field(default="admin", description="Actor making the change.")

    model_config = {"extra": "allow"}


class InitiatePaymentRequest(BaseModel):
    """POST /api/v1/payments/initiate/"""

    booking_id: str | None = Field(default=None, description="Booking ID if paying for a booking.")
    voucher_id: str | None = Field(default=None, description="Voucher ID if paying for a voucher.")
    payment_for: str = Field(
        default=PaymentFor.BRANCH_SERVICE.value,
        description="Purpose: branch_service, home_service, gift_voucher, product_items",
    )
    provider: str = Field(default=PaymentProvider.MYFATOORAH.value, description="Payment provider to use.")
    payment_method: str = "card"  # card | knet | apple_pay | google_pay


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
    customer_id: str
    customer_data: dict[str, Any] | None = None
    sender_id: str | None = None

    service_id: str | None = None
    branch_id: str | None = None
    service_arrangement_id: str | None = None

    addons_price: str | None = None
    extra_time: int | None = None

    total_amount: str
    total_duration: int
    currency: str
    country: str | None = None

    status: str | None = None
    paid_at: datetime | None = None

    recipient_id: str | None = None
    recipient_phone: str | None = None

    booking_id: str | None = None
    voucher_id: str | None = None
    product_order_id: str | None = None

    invoice_id: str | None = None
    invoice_value: str | None = None
    payment_url: str | None = None
    transaction_id: str | None = None
    track_id: str | None = None
    reference_id: str | None = None
    transaction_status: str | None = None
    transaction_date: str | None = None

    payment_method: str | None = None
    payment_through: str | None = None
    payment_provider: str | None = None
    payment_gateway: str | None = None
    payment_for: str | None = None
    payment_id: str | None = None

    created_by: str | None = None
    created_at: datetime


class PaymentDetailResponse(BaseModel):
    """Full detail of a payment record for finance dashboards and audits."""

    id: str
    customer_id: str
    customer_data: dict[str, Any] | None = None
    sender_id: str | None = None
    sender_data: dict[str, Any] | None = None

    service_id: str | None = None
    service_data: dict[str, Any] | None = None
    branch_id: str | None = None
    branch_data: dict[str, Any] | None = None
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] | None = None

    addons: Any | None = None
    addons_price: str | None = None
    extra_time: int | None = None
    price_for_extra_time: str | None = None

    total_amount: str
    total_duration: int
    currency: str
    country: str | None = None

    status: str | None = None
    paid_at: datetime | None = None

    recipient_id: str | None = None
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] | None = None

    booking_id: str | None = None
    booking_data: dict[str, Any] | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    product_order_id: str | None = None
    product_order_items: Any | None = None

    invoice_id: str | None = None
    invoice_value: str | None = None
    payment_url: str | None = None
    transaction_id: str | None = None
    track_id: str | None = None
    reference_id: str | None = None
    transaction_status: str | None = None
    transaction_date: str | None = None
    receipt_image: str | None = None

    payment_method: str | None = None
    payment_through: str | None = None
    payment_provider: str | None = None
    payment_gateway: str | None = None
    payment_for: str | None = None
    payment_id: str | None = None
    payment_data: dict[str, Any] | None = None

    created_by: str | None = None
    created_at: datetime

    status_history: list[PaymentStatusHistoryItem] | None = None


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
    payment_for: str = PaymentFor.BRANCH_SERVICE.value
    provider: str
    payment_url: str
    total_amount: str
    currency: str
    expires_at: datetime | None = None


class PaymentStatusResponse(BaseModel):
    """Payment status for a booking or voucher."""

    payment_id: str
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = PaymentFor.BRANCH_SERVICE.value
    payment_provider: str
    status: str | None = None
    total_amount: str
    currency: str
    payment_method: str | None = None
    reference_id: str | None = None
    created_at: datetime


class WebhookVerifyRequest(BaseModel):
    """Internal-use: triggered by webhook to verify and update payment."""

    provider: str
    provider_payment_id: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
