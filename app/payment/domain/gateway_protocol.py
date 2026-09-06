"""
app/payment/domain/gateway_protocol.py
────────────────────────────────────────
Payment gateway abstraction using Python Protocol (structural subtyping).

The booking domain depends ONLY on this Protocol — never on a concrete provider.
This ensures the Booking and Payment domains remain independently extractable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass
class CreatePaymentRequest:
    """Provider-agnostic payment creation request."""

    customer_id: str
    amount: Decimal
    currency: str
    payment_method: str
    customer_name: str
    customer_email: str
    customer_phone: str
    description: str
    callback_url: str
    success_url: str
    error_url: str
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    metadata: dict[str, Any] | None = None


@dataclass
class CreatePaymentResponse:
    """Provider-agnostic payment creation response."""

    provider_payment_id: str       # Provider's internal ID
    payment_url: str               # Redirect URL for the customer
    provider_reference: str        # Reference to track in webhook
    raw_response: dict[str, Any]   # Full provider response for logging


@dataclass
class VerifyPaymentResponse:
    """Provider-agnostic payment verification result."""

    is_successful: bool
    provider_payment_id: str
    provider_status: str           # Provider's own status string
    amount_charged: Decimal | None
    currency: str | None
    failure_reason: str | None
    raw_response: dict[str, Any]


@dataclass
class RefundRequest:
    """Provider-agnostic refund request."""

    provider_payment_id: str
    amount: Decimal | None = None  # None = full refund
    reason: str = ""


@dataclass
class RefundResponse:
    """Provider-agnostic refund response."""

    is_successful: bool
    provider_refund_id: str | None
    failure_reason: str | None
    raw_response: dict[str, Any]


@runtime_checkable
class PaymentGateway(Protocol):
    """
    Abstract payment gateway interface.

    All payment provider implementations must satisfy this protocol.
    The Protocol is runtime-checkable to support isinstance() in tests.
    """

    @property
    def provider_name(self) -> str:
        """Human-readable provider identifier."""
        ...

    async def create_payment(
        self,
        request: CreatePaymentRequest,
    ) -> CreatePaymentResponse:
        """
        Initiate a payment session and return a redirect URL.

        The customer will be redirected to payment_url to complete payment.
        """
        ...

    async def verify_payment(
        self,
        provider_payment_id: str,
    ) -> VerifyPaymentResponse:
        """
        Verify the status of a payment by provider reference.

        Called after the customer completes or cancels payment.
        """
        ...

    async def refund_payment(
        self,
        request: RefundRequest,
    ) -> RefundResponse:
        """Issue a full or partial refund for a completed payment."""
        ...
