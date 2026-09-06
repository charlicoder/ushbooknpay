"""
app/payment/infrastructure/providers/tap_provider.py
──────────────────────────────────────────────────────
Tap Payments gateway provider.

API Reference: https://www.tap.company/developers/

Key endpoints:
- POST /v2/charges        → create a charge and get a redirect URL
- GET  /v2/charges/{id}   → verify charge status
- POST /v2/refunds        → issue a refund

Tap uses a secret key (sk_live_... / sk_test_...) in Authorization header.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import PaymentProviderError, RefundError
from app.core.logging import get_logger
from app.payment.domain.gateway_protocol import (
    CreatePaymentRequest,
    CreatePaymentResponse,
    PaymentGateway,
    RefundRequest,
    RefundResponse,
    VerifyPaymentResponse,
)

logger = get_logger(__name__)


class TapProvider:
    """
    Tap Payments gateway implementation.

    Satisfies the PaymentGateway Protocol without inheritance.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._settings = settings or get_settings()

    @property
    def provider_name(self) -> str:
        return "tap"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.TAP_SECRET_KEY}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.TAP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request to Tap API."""
        url = self._url(path)
        try:
            response = await self._http.request(
                method,
                url,
                json=payload,
                params=params,
                headers=self._headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "tap_http_error",
                path=path,
                status=exc.response.status_code,
            )
            raise PaymentProviderError(
                f"Tap API error ({exc.response.status_code})",
                detail=exc.response.text[:500],
                code="TAP_API_ERROR",
            )
        except httpx.TimeoutException:
            raise PaymentProviderError("Tap API timed out.", code="TAP_TIMEOUT")

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        """Create a Tap charge and return the redirect URL."""
        payload: dict[str, Any] = {
            "amount": float(request.amount),
            "currency": request.currency,
            "description": request.description,
            "reference": {
                "transaction": request.booking_id or request.voucher_id or request.customer_id,
                "order": request.booking_id or request.voucher_id or request.customer_id,
            },
            "receipt": {"email": True, "sms": True},
            "customer": {
                "first_name": request.customer_name.split(" ")[0],
                "last_name": " ".join(request.customer_name.split(" ")[1:]) or "",
                "email": request.customer_email,
                "phone": {
                    "country_code": "965",
                    "number": request.customer_phone.lstrip("+").lstrip("965"),
                },
            },
            "source": {"id": "src_all"},  # Show all payment methods
            "redirect": {
                "url": request.callback_url,
            },
            "post": {
                "url": request.callback_url,  # Webhook URL
            },
            "metadata": request.metadata or {},
        }

        response = await self._request("POST", "/charges", payload)

        charge_id: str = response.get("id", "")
        transaction = response.get("transaction", {})
        pay_url: str = transaction.get("url", "")

        logger.info(
            "tap_charge_created",
            booking_id=request.booking_id,
            voucher_id=request.voucher_id,
            charge_id=charge_id,
        )

        return CreatePaymentResponse(
            provider_payment_id=charge_id,
            payment_url=pay_url,
            provider_reference=charge_id,
            raw_response=response,
        )

    async def verify_payment(
        self, provider_payment_id: str
    ) -> VerifyPaymentResponse:
        """Retrieve charge details to verify payment status."""
        response = await self._request("GET", f"/charges/{provider_payment_id}")

        tap_status: str = response.get("status", "").upper()
        is_captured = tap_status == "CAPTURED"

        amount_charged = None
        if is_captured:
            try:
                amount_charged = Decimal(str(response.get("amount", 0)))
            except Exception:
                pass

        return VerifyPaymentResponse(
            is_successful=is_captured,
            provider_payment_id=provider_payment_id,
            provider_status=tap_status,
            amount_charged=amount_charged,
            currency=response.get("currency"),
            failure_reason=None if is_captured else response.get("response", {}).get("message"),
            raw_response=response,
        )

    async def refund_payment(self, request: RefundRequest) -> RefundResponse:
        """Issue a refund for a Tap charge."""
        payload: dict[str, Any] = {
            "charge_id": request.provider_payment_id,
            "reason": request.reason or "Booking cancellation",
        }
        if request.amount is not None:
            payload["amount"] = float(request.amount)

        response = await self._request("POST", "/refunds", payload)

        refund_status = response.get("status", "").upper()
        is_refunded = refund_status in {"INITIATED", "APPROVED"}

        if not is_refunded:
            raise RefundError(
                f"Tap refund failed: {response.get('response', {}).get('message')}",
                code="TAP_REFUND_FAILED",
            )

        logger.info(
            "tap_refund_issued",
            provider_payment_id=request.provider_payment_id,
            refund_id=response.get("id"),
        )

        return RefundResponse(
            is_successful=True,
            provider_refund_id=response.get("id"),
            failure_reason=None,
            raw_response=response,
        )
