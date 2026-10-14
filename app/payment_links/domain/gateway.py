"""
app/payment_links/domain/gateway.py
────────────────────────────────────
Provider-agnostic abstraction for *hosted payment links*.

Application services depend ONLY on :class:`PaymentLinkGateway` and the DTOs in
this module. A new payment system (Tap, Deema, Stripe, …) is added by writing a
new adapter and registering it in :mod:`app.payment_links.domain.registry` —
no change to the services, API or webhook processing is required (OCP / DIP).

Interface segregation: the capabilities are split in small ABCs so a provider
that cannot refund (or has no webhooks) only implements what it supports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from app.payment.domain.gateway_protocol import RefundRequest, RefundResponse


class GatewayPaymentStatus(str, Enum):
    """Normalised payment outcome, independent of the provider's vocabulary."""

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


class WebhookEventKind(str, Enum):
    PAYMENT_STATUS = "payment_status"
    REFUND_STATUS = "refund_status"
    IGNORED = "ignored"


@dataclass(frozen=True)
class PaymentLinkRequest:
    """Everything a gateway needs to issue a shareable payment link."""

    amount: Decimal
    currency: str
    reference: str  # our Payment.id — echoed back by the gateway
    description: str
    customer_name: str
    customer_phone: str | None = None
    customer_email: str | None = None
    expires_at: datetime | None = None
    callback_url: str | None = None
    error_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentLinkResult:
    gateway_invoice_id: str
    payment_url: str
    raw_response: dict[str, Any]
    raw_request: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayPaymentState:
    """Server-verified state of a payment, pulled from the gateway."""

    status: GatewayPaymentStatus
    invoice_id: str | None = None
    payment_id: str | None = None
    transaction_id: str | None = None
    track_id: str | None = None
    reference_id: str | None = None
    payment_method: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    failure_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayWebhookEvent:
    """Normalised webhook event produced by a gateway adapter."""

    kind: WebhookEventKind
    event_reference: str | None
    invoice_id: str | None = None
    status: GatewayPaymentStatus | None = None
    state: GatewayPaymentState | None = None
    refund_id: str | None = None
    refund_status: str | None = None
    refund_amount: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentLinkCreator(ABC):
    @abstractmethod
    async def create_link(self, request: PaymentLinkRequest) -> PaymentLinkResult: ...


class PaymentStatusReader(ABC):
    @abstractmethod
    async def get_payment_state(self, invoice_id: str) -> GatewayPaymentState: ...


class PaymentRefunder(ABC):
    @abstractmethod
    async def refund_payment(self, request: RefundRequest) -> RefundResponse: ...


class WebhookHandler(ABC):
    @abstractmethod
    def verify_signature(self, headers: Mapping[str, str], payload: dict[str, Any]) -> bool:
        """Return True only if the request authentically comes from the gateway."""

    @abstractmethod
    def parse_event(self, payload: dict[str, Any]) -> GatewayWebhookEvent: ...


class PaymentLinkGateway(PaymentLinkCreator, PaymentStatusReader, PaymentRefunder, WebhookHandler, ABC):
    """Full-featured gateway. Concrete adapters set :attr:`name` and
    :attr:`provider_label` (stored in ``payments.payment_provider``)."""

    name: str
    provider_label: str

    async def cancel_link(self, gateway_invoice_id: str) -> dict[str, Any]:
        """Send cancel request to gateway for an open invoice / payment link."""
        return {"success": True}
