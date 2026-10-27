"""
app/payment/infrastructure/providers/deema_provider.py
──────────────────────────────────────────────────────
Deema BNPL payment provider (powered by Tap Payments Charges API).

API Reference: https://developers.tap.company/

Key endpoints:
- POST /v2/charges        → create a Deema charge (source.id = "src_deema")
- GET  /v2/charges/{id}   → verify charge status
- POST /v2/refunds        → issue a refund

Uses DEEMA_SECRET_KEY (or TAP_SECRET_KEY) and DEEMA_MERCHANT_ID (or TAP_MERCHANT_ID).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.payment.domain.gateway_protocol import (
    CreatePaymentRequest,
    CreatePaymentResponse,
    RefundRequest,
    RefundResponse,
    VerifyPaymentResponse,
)
from app.payment_links.domain.gateway import GatewayPaymentStatus, PaymentLinkRequest
from app.payment_links.infrastructure.deema_gateway import DeemaGateway

logger = get_logger(__name__)


class DeemaProvider:
    """
    Deema payment provider implementation.

    Satisfies the PaymentGateway Protocol and delegates HTTP interaction to
    :class:`DeemaGateway` (DRY / Single Responsibility).
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._settings = settings or get_settings()
        self._gateway = DeemaGateway(http_client=self._http, settings=self._settings)

    @property
    def provider_name(self) -> str:
        return "deema"

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        """Create a Deema charge via Tap Charges API and return the redirect URL."""
        reference = request.booking_id or request.voucher_id or request.customer_id
        result = await self._gateway.create_link(
            PaymentLinkRequest(
                amount=request.amount,
                currency=request.currency,
                reference=reference,
                description=request.description or "USH Spa",
                customer_name=request.customer_name or "Customer",
                customer_phone=request.customer_phone,
                customer_email=request.customer_email,
                callback_url=request.callback_url,
                error_url=request.error_url,
                metadata=request.metadata or {},
            )
        )
        return CreatePaymentResponse(
            provider_payment_id=result.gateway_invoice_id,
            payment_url=result.payment_url,
            provider_reference=result.gateway_invoice_id,
            raw_response=result.raw_response,
        )

    async def initiate_payment(self, request: Any) -> dict[str, Any]:
        """Compatibility helper for POST /api/v1/payments/initiate/."""
        customer_phone = getattr(request, "customer_phone", None) or getattr(request, "customer_mobile", "") or ""
        reference = (
            getattr(request, "booking_id", None)
            or getattr(request, "voucher_id", None)
            or getattr(request, "customer_id", "")
        )
        result = await self._gateway.create_link(
            PaymentLinkRequest(
                amount=request.amount,
                currency=getattr(request, "currency", "KWD") or "KWD",
                reference=str(reference),
                description=getattr(request, "description", None) or "USH Spa Booking",
                customer_name=getattr(request, "customer_name", None) or "Customer",
                customer_phone=str(customer_phone),
                customer_email=getattr(request, "customer_email", None),
                callback_url=getattr(request, "callback_url", None) or None,
                error_url=getattr(request, "error_url", None) or None,
                metadata=getattr(request, "metadata", None) or {},
            )
        )
        return {
            "payment_id": result.gateway_invoice_id,
            "payment_url": result.payment_url,
            "raw_response": result.raw_response,
        }

    async def verify_payment(
        self, provider_payment_id: str
    ) -> VerifyPaymentResponse:
        """Retrieve charge details from Tap to verify Deema payment status."""
        state = await self._gateway.get_payment_state(provider_payment_id)
        is_paid = state.status == GatewayPaymentStatus.PAID
        return VerifyPaymentResponse(
            is_successful=is_paid,
            provider_payment_id=state.invoice_id or provider_payment_id,
            provider_status=state.status.value.upper(),
            amount_charged=state.amount if is_paid else None,
            currency=state.currency,
            failure_reason=state.failure_reason,
            raw_response=state.raw,
        )

    async def refund_payment(self, request: RefundRequest) -> RefundResponse:
        """Issue a refund for a Deema charge via Tap Refunds API."""
        return await self._gateway.refund_payment(request)
