"""
app/payment/infrastructure/providers/myfatoorah_provider.py
─────────────────────────────────────────────────────────────
MyFatoorah payment gateway provider.

API Reference: https://docs.myfatoorah.com/docs/

Key flows:
1. Create payment: POST /v2/InitiatePayment → POST /v2/ExecutePayment
2. Verify payment: POST /v2/GetPaymentStatus
3. Refund:         POST /v2/MakeRefund

MyFatoorah uses a single API key (Bearer token) for authentication.
All amounts must be sent in the currency's precision (e.g. 3 decimals for KWD).

IMPORTANT: Never log the API key or full card data from provider responses.
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

# MyFatoorah payment method codes
_PAYMENT_METHOD_MAP: dict[str, int] = {
    "knet": 1,
    "card": 2,
    "apple_pay": 11,
    "google_pay": 12,
}


class MyFatoorahProvider:
    """
    MyFatoorah payment gateway implementation.

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
        return "myfatoorah"

    def _headers(self) -> dict[str, str]:
        # Never log this method's output
        return {
            "Authorization": f"Bearer {self._settings.MYFATOORAH_API_KEY}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.MYFATOORAH_BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute a POST to MyFatoorah API."""
        url = self._url(path)
        try:
            response = await self._http.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "myfatoorah_http_error",
                path=path,
                status=exc.response.status_code,
            )
            raise PaymentProviderError(
                f"MyFatoorah API error ({exc.response.status_code})",
                detail=exc.response.text[:500],
                code="MYFATOORAH_API_ERROR",
            )
        except httpx.TimeoutException:
            logger.error("myfatoorah_timeout", path=path)
            raise PaymentProviderError(
                "MyFatoorah API timed out.",
                code="MYFATOORAH_TIMEOUT",
            )

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        """
        Create a MyFatoorah payment session.

        Step 1: InitiatePayment — get the supported payment methods.
        Step 2: ExecutePayment — create the payment and get the redirect URL.
        """
        # Step 1: InitiatePayment
        initiate_payload = {
            "InvoiceAmount": float(request.amount),
            "CurrencyIso": request.currency,
        }
        initiate_response = await self._post("v2/InitiatePayment", initiate_payload)

        if not initiate_response.get("IsSuccess"):
            raise PaymentProviderError(
                f"MyFatoorah InitiatePayment failed: {initiate_response.get('Message')}",
                code="MYFATOORAH_INITIATE_FAILED",
            )

        method_str = (request.payment_method or "card").strip().lower()
        payment_method_id = _PAYMENT_METHOD_MAP.get(method_str, 2)

        # Step 2: ExecutePayment
        execute_payload = {
            "PaymentMethodId": payment_method_id,
            "CustomerName": request.customer_name,
            "CustomerEmail": request.customer_email,
            "MobileCountryCode": "+965",
            "CustomerMobile": request.customer_phone.lstrip("+"),
            "InvoiceValue": float(request.amount),
            "DisplayCurrencyIso": request.currency,
            "CallBackUrl": request.callback_url,
            "ErrorUrl": request.error_url,
            "Language": "en",
            "CustomerReference": request.booking_id or request.voucher_id or request.customer_id,
            "InvoiceItems": [
                {
                    "ItemName": request.description,
                    "Quantity": 1,
                    "UnitPrice": float(request.amount),
                }
            ],
        }

        execute_response = await self._post("v2/ExecutePayment", execute_payload)

        if not execute_response.get("IsSuccess"):
            raise PaymentProviderError(
                f"MyFatoorah ExecutePayment failed: {execute_response.get('Message')}",
                code="MYFATOORAH_EXECUTE_FAILED",
            )

        data = execute_response.get("Data", {})
        invoice_id = str(data.get("InvoiceId", ""))

        logger.info(
            "myfatoorah_payment_created",
            booking_id=request.booking_id,
            voucher_id=request.voucher_id,
            invoice_id=invoice_id,
        )

        return CreatePaymentResponse(
            provider_payment_id=invoice_id,
            payment_url=data.get("PaymentURL", ""),
            provider_reference=invoice_id,
            raw_response=execute_response,
        )

    async def verify_payment(
        self, provider_payment_id: str
    ) -> VerifyPaymentResponse:
        """Verify payment status via GetPaymentStatus."""
        payload = {"Key": provider_payment_id, "KeyType": "InvoiceId"}
        response = await self._post("v2/GetPaymentStatus", payload)

        if not response.get("IsSuccess"):
            return VerifyPaymentResponse(
                is_successful=False,
                provider_payment_id=provider_payment_id,
                provider_status="ERROR",
                amount_charged=None,
                currency=None,
                failure_reason=response.get("Message"),
                raw_response=response,
            )

        data = response.get("Data", {})
        invoice_status: str = data.get("InvoiceStatus", "").lower()
        is_paid = invoice_status == "paid"

        amount_charged = None
        if is_paid:
            try:
                amount_charged = Decimal(str(data.get("InvoiceValue", 0)))
            except Exception:
                pass

        return VerifyPaymentResponse(
            is_successful=is_paid,
            provider_payment_id=provider_payment_id,
            provider_status=invoice_status.upper(),
            amount_charged=amount_charged,
            currency=data.get("CurrencyIso"),
            failure_reason=None if is_paid else data.get("UserDefinedField"),
            raw_response=response,
        )

    async def refund_payment(self, request: RefundRequest) -> RefundResponse:
        """Issue a full or partial refund via MakeRefund."""
        payload: dict[str, Any] = {
            "Key": request.provider_payment_id,
            "KeyType": "InvoiceId",
            "RefundChargeOnCustomer": False,
            "ServiceChargOnCustomer": False,
            "Amount": float(request.amount) if request.amount else None,
            "Comment": request.reason or "Booking cancellation refund",
        }
        # Remove None amount for full refund
        if payload["Amount"] is None:
            del payload["Amount"]

        response = await self._post("v2/MakeRefund", payload)

        if not response.get("IsSuccess"):
            raise RefundError(
                f"MyFatoorah refund failed: {response.get('Message')}",
                code="MYFATOORAH_REFUND_FAILED",
            )

        logger.info(
            "myfatoorah_refund_issued",
            provider_payment_id=request.provider_payment_id,
        )

        return RefundResponse(
            is_successful=True,
            provider_refund_id=str(response.get("Data", {}).get("RefundId", "")),
            failure_reason=None,
            raw_response=response,
        )
