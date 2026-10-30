"""
app/payment_links/infrastructure/myfatoorah_gateway.py
───────────────────────────────────────────────────────
MyFatoorah adapter for :class:`PaymentLinkGateway`.

Docs: https://docs.myfatoorah.com/docs/

* Link creation  : POST /v2/SendPayment   (NotificationOption=LNK → MyFatoorah
                   does not message the customer; ushdesk shares the link)
* Status check   : POST /v2/GetPaymentStatus   (Key=InvoiceId)
* Refund         : POST /v2/MakeRefund
* Webhook V2     : header ``MyFatoorah-Signature`` = base64(HMAC-SHA256(secret,
                   "k=v,k=v,…")) over an event-specific, ordered set of fields.

SECURITY: the API key and webhook secret are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
from datetime import timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import httpx


_CUSTOMER_NAME_DISALLOWED = re.compile(r"[^\w\s.\-']", re.UNICODE)


def sanitize_customer_name(name: str | None, default: str = "Customer") -> str:
    """MyFatoorah rejects punctuation (e.g. ':' in "إلى : خالتي") in CustomerName.

    Keeps letters (any script, incl. Arabic), digits, spaces, '.', '-' and "'";
    drops everything else and collapses whitespace.
    """
    cleaned = _CUSTOMER_NAME_DISALLOWED.sub(" ", name or "").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:100]
    return cleaned or default

from app.core.config import Settings
from app.core.exceptions import PaymentProviderError, RefundError
from app.core.logging import get_logger
from app.payment.domain.gateway_protocol import RefundRequest, RefundResponse
from app.payment_links.domain.gateway import (
    GatewayPaymentState,
    GatewayPaymentStatus,
    GatewayWebhookEvent,
    PaymentLinkGateway,
    PaymentLinkRequest,
    PaymentLinkResult,
    WebhookEventKind,
)
from app.payment_links.domain.registry import register_gateway

logger = get_logger(__name__)

_KUWAIT = ZoneInfo("Asia/Kuwait")
_SIGNATURE_HEADER = "myfatoorah-signature"

# Ordered fields used to build the signature string, per webhook event code
_PAYMENT_SIGNATURE_FIELDS = (
    "Invoice.Id",
    "Invoice.Status",
    "Transaction.Status",
    "Transaction.PaymentId",
    "Invoice.ExternalIdentifier",
)
_REFUND_SIGNATURE_FIELDS = (
    "Refund.Id",
    "Refund.Status",
    "Amount.ValueInBaseCurrency",
    "ReferencedInvoice.Id",
)

_KNOWN_DIAL_CODES = ("+965", "+966", "+971", "+973", "+974", "+968", "+962", "+20", "+91", "+44", "+1")


_FLAT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "Invoice.Id": ("InvoiceId", "invoice_id"),
    "Invoice.Status": ("InvoiceStatus", "invoice_status"),
    "Invoice.ExternalIdentifier": ("ExternalIdentifier", "CustomerReference", "customer_reference"),
    "Transaction.Status": ("TransactionStatus", "transaction_status"),
    "Transaction.PaymentId": ("PaymentId", "payment_id"),
    "Transaction.Id": ("TransactionId", "transaction_id"),
    "Transaction.TrackId": ("TrackId", "track_id"),
    "Transaction.ReferenceId": ("ReferenceId", "reference_id"),
    "Transaction.PaymentMethod": ("PaymentMethod", "PaymentGateway", "payment_method"),
    "Amount.ValueInBaseCurrency": ("InvoiceValueInBaseCurrency", "InvoiceValue", "Amount"),
    "Amount.BaseCurrency": ("BaseCurrency", "Currency"),
    "Refund.Id": ("RefundId", "RefundReference"),
    "Refund.Status": ("RefundStatus",),
    "Refund.InvoiceId": ("RefundInvoiceId", "InvoiceId"),
    "ReferencedInvoice.Id": ("ReferencedInvoiceId", "InvoiceId"),
}


def _dig(data: Mapping[str, Any] | None, dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, Mapping):
            cur = None
            break
        cur = cur.get(part)
    if cur is not None and cur != "":
        return cur
    if isinstance(data, Mapping) and dotted in _FLAT_FALLBACKS:
        for fallback in _FLAT_FALLBACKS[dotted]:
            val = data.get(fallback)
            if val is not None and val != "":
                return val
    return cur


def _to_str(value: Any) -> str:
    return "" if value is None else str(value)


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def split_phone(phone: str | None, default_code: str) -> tuple[str, str]:
    """Split a free-form phone into (+dialcode, national number)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return default_code, ""
    raw = (phone or "").strip()
    if raw.startswith("+") or raw.startswith("00"):
        normalised = "+" + digits.removeprefix("00") if raw.startswith("00") else "+" + digits
        for code in _KNOWN_DIAL_CODES:
            if normalised.startswith(code):
                return code, normalised[len(code):][:11]
    code_digits = default_code.lstrip("+")
    if digits.startswith(code_digits) and len(digits) > len(code_digits) + 5:
        digits = digits[len(code_digits):]
    return default_code, digits[:11]


class MyFatoorahGateway(PaymentLinkGateway):
    name = "myfatoorah"
    provider_label = "MyFatoorah"

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._settings = settings

    # ── HTTP plumbing ───────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.MYFATOORAH_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.MYFATOORAH_BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.MYFATOORAH_API_KEY:
            raise PaymentProviderError(
                "MyFatoorah is not configured (missing API key).", code="MYFATOORAH_NOT_CONFIGURED"
            )
        headers = self._headers()
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._http.post(
                    self._url(path), json=payload, headers=headers, timeout=30.0
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.status_code >= 400 and not body:
                    if attempt == 0 and response.status_code >= 500:
                        await asyncio.sleep(0.5)
                        continue
                    logger.error("myfatoorah_http_error", path=path, status=response.status_code)
                    raise PaymentProviderError(
                        f"MyFatoorah API error ({response.status_code}).", code="MYFATOORAH_API_ERROR"
                    )
                return body
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    logger.error("myfatoorah_timeout", path=path)
                    raise PaymentProviderError("MyFatoorah API timed out.", code="MYFATOORAH_TIMEOUT") from exc
                logger.error("myfatoorah_network_error", path=path, error=type(exc).__name__)
                raise PaymentProviderError("MyFatoorah is unreachable.", code="MYFATOORAH_UNREACHABLE") from exc
            except PaymentProviderError:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise
        if last_exc:
            raise last_exc
        return {}

    @staticmethod
    def _error_message(body: Mapping[str, Any]) -> str:
        message = body.get("Message")
        field_errors = body.get("ValidationErrors") or []
        if field_errors:
            detail = "; ".join(
                f"{e.get('Name')}: {e.get('Error')}" for e in field_errors if isinstance(e, Mapping)
            )
            return f"{message} ({detail})" if message else detail
        return str(message or "Unknown error")

    # ── PaymentLinkCreator ──────────────────────────────────────────────
    async def create_link(self, request: PaymentLinkRequest) -> PaymentLinkResult:
        code, number = split_phone(request.customer_phone, self._settings.MYFATOORAH_DEFAULT_COUNTRY_CODE)
        payload: dict[str, Any] = {
            "NotificationOption": "LNK",
            "InvoiceValue": float(request.amount),
            "CustomerName": sanitize_customer_name(request.customer_name),
            "DisplayCurrencyIso": request.currency,
            "Language": "en",
            "CustomerReference": request.reference,
            "UserDefinedField": request.reference,
            "InvoiceItems": [
                {"ItemName": request.description[:100] or "USH Spa", "Quantity": 1, "UnitPrice": float(request.amount)}
            ],
        }
        if number:
            payload["MobileCountryCode"] = code
            payload["CustomerMobile"] = number
        if request.customer_email:
            payload["CustomerEmail"] = request.customer_email
        callback = (
            request.callback_url
            or self._settings.MYFATOORAH_CALLBACK_URL
            or self._settings.MYFATOORAH_SUCCESS_URL
        )
        error = (
            request.error_url
            or self._settings.MYFATOORAH_CALLBACK_URL
            or self._settings.MYFATOORAH_ERROR_URL
        )
        if callback:
            payload["CallBackUrl"] = callback
        if error:
            payload["ErrorUrl"] = error
        if request.expires_at is not None:
            expires = request.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            payload["ExpiryDate"] = expires.astimezone(_KUWAIT).strftime("%Y-%m-%dT%H:%M:%S")

        body = await self._post("v2/SendPayment", payload)
        if not body.get("IsSuccess"):
            logger.warning("myfatoorah_send_payment_rejected", message=self._error_message(body))
            raise PaymentProviderError(
                f"MyFatoorah rejected the payment link: {self._error_message(body)}",
                code="MYFATOORAH_SEND_PAYMENT_FAILED",
            )
        data = body.get("Data") or {}
        invoice_id = _to_str(data.get("InvoiceId"))
        url = data.get("InvoiceURL") or data.get("PaymentURL") or ""
        if not invoice_id or not url:
            raise PaymentProviderError("MyFatoorah returned an incomplete response.", code="MYFATOORAH_BAD_RESPONSE")
        logger.info("myfatoorah_link_created", invoice_id=invoice_id, reference=request.reference)
        return PaymentLinkResult(
            gateway_invoice_id=invoice_id, payment_url=url, raw_response=body, raw_request=payload
        )

    # ── PaymentStatusReader ─────────────────────────────────────────────
    async def get_payment_state(self, invoice_id: str) -> GatewayPaymentState:
        val = str(invoice_id).strip()
        is_payment_id = len(val) >= 14 or val.startswith("0")
        primary_key_type = "PaymentId" if is_payment_id else "InvoiceId"
        secondary_key_type = "InvoiceId" if is_payment_id else "PaymentId"

        body = None
        for ktype in (primary_key_type, secondary_key_type):
            try:
                res_body = await self._post("v2/GetPaymentStatus", {"Key": val, "KeyType": ktype})
                if isinstance(res_body, Mapping) and res_body.get("IsSuccess"):
                    body = res_body
                    break
            except Exception as exc:
                logger.info(
                    "myfatoorah_status_probe_attempt",
                    key=val,
                    key_type=ktype,
                    error=str(exc),
                )

        if not body or not body.get("IsSuccess"):
            # When querying by InvoiceId before a payment attempt has occurred, MyFatoorah's
            # test environment returns 500 because no transaction record exists yet.
            # In that case, treat it as still PENDING rather than crashing the sync / polling loop.
            if not is_payment_id:
                logger.info("myfatoorah_status_pending_fallback", invoice_id=val)
                return GatewayPaymentState(
                    status=GatewayPaymentStatus.PENDING,
                    invoice_id=val,
                    payment_id=None,
                    transaction_id=None,
                    track_id=None,
                    reference_id=None,
                    payment_method=None,
                    amount=None,
                    currency=None,
                    failure_reason=None,
                    raw=body or {},
                )
            raise PaymentProviderError(
                f"MyFatoorah status lookup failed: {self._error_message(body or {})}",
                code="MYFATOORAH_STATUS_FAILED",
            )
        data = body.get("Data") or {}
        transactions = [t for t in (data.get("InvoiceTransactions") or []) if isinstance(t, Mapping)]
        invoice_status = _to_str(data.get("InvoiceStatus")).strip().lower()

        def _ok(tx: Mapping[str, Any]) -> bool:
            return _to_str(tx.get("TransactionStatus")).strip().lower() in {"succss", "success", "succeeded"}

        success_tx = next((t for t in transactions if _ok(t)), None)
        matched_tx = next(
            (
                t
                for t in transactions
                if _to_str(t.get("PaymentId")) == str(invoice_id)
                or _to_str(t.get("TransactionId")) == str(invoice_id)
            ),
            None,
        )
        if invoice_status == "paid" or success_tx is not None:
            status = GatewayPaymentStatus.PAID
        elif invoice_status in {"expired"}:
            status = GatewayPaymentStatus.EXPIRED
        elif invoice_status in {"canceled", "cancelled"}:
            status = GatewayPaymentStatus.CANCELED
        elif matched_tx and _to_str(matched_tx.get("TransactionStatus")).lower() == "failed":
            status = GatewayPaymentStatus.FAILED
        elif transactions and all(not _ok(t) and _to_str(t.get("TransactionStatus")).lower() == "failed" for t in transactions):
            status = GatewayPaymentStatus.FAILED
        else:
            status = GatewayPaymentStatus.PENDING

        tx = success_tx or matched_tx or (transactions[-1] if transactions else {})
        raw_cur = _to_str(tx.get("Currency") or data.get("DisplayCurrencyIso") or data.get("CurrencyIso") or "").strip().upper()
        norm_cur = "KWD" if raw_cur in ("KD", "K.D.", "K.D", "د.ك", "KWD") else (raw_cur or None)
        return GatewayPaymentState(
            status=status,
            invoice_id=_to_str(data.get("InvoiceId")) or invoice_id,
            payment_id=_to_str(tx.get("PaymentId")) or None,
            transaction_id=_to_str(tx.get("TransactionId")) or None,
            track_id=_to_str(tx.get("TrackId")) or None,
            reference_id=_to_str(tx.get("ReferenceId")) or None,
            payment_method=_to_str(tx.get("PaymentGateway")) or None,
            amount=_to_decimal(data.get("InvoiceValue")),
            currency=norm_cur,
            failure_reason=_to_str(tx.get("Error")) or None if status == GatewayPaymentStatus.FAILED else None,
            raw=body,
        )

    # ── PaymentRefunder ─────────────────────────────────────────────────
    async def refund_payment(self, request: RefundRequest) -> RefundResponse:
        payload: dict[str, Any] = {
            "Key": request.provider_payment_id,
            "KeyType": "InvoiceId",
            "RefundChargeOnCustomer": False,
            "ServiceChargeOnCustomer": False,
            "Comment": (request.reason or "Refund")[:200],
        }
        if request.amount is not None:
            payload["Amount"] = float(request.amount)
        body = await self._post("v2/MakeRefund", payload)
        if not body.get("IsSuccess"):
            raise RefundError(
                f"MyFatoorah refund failed: {self._error_message(body)}", code="MYFATOORAH_REFUND_FAILED"
            )
        data = body.get("Data") or {}
        logger.info("myfatoorah_refund_requested", invoice_id=request.provider_payment_id)
        return RefundResponse(
            is_successful=True,
            provider_refund_id=_to_str(data.get("RefundId") or data.get("RefundReference")) or None,
            failure_reason=None,
            raw_response=body,
        )

    # ── Cancel Payment Link / Invoice ───────────────────────────────────
    async def cancel_link(self, gateway_invoice_id: str) -> dict[str, Any]:
        """Send cancel request to MyFatoorah to cancel previous payment link / invoice."""
        if not gateway_invoice_id:
            return {"IsSuccess": False, "Message": "Missing gateway invoice ID"}
        val = str(gateway_invoice_id).strip()
        payload = {"Key": val, "KeyType": "InvoiceId", "InvoiceId": val}
        body: dict[str, Any] = {}
        logger.info("myfatoorah_cancel_link_request", gateway_invoice_id=val)

        # 1. Try /v2/CancelInvoice
        try:
            body = await self._post("v2/CancelInvoice", payload)
            if isinstance(body, dict) and body.get("IsSuccess"):
                return body
        except Exception as exc:
            logger.info("myfatoorah_cancel_invoice_endpoint_notice", error=str(exc))

        # 2. Try /v2/CancelPayment
        try:
            body = await self._post("v2/CancelPayment", payload)
            if isinstance(body, dict) and body.get("IsSuccess"):
                return body
        except Exception as exc2:
            logger.info("myfatoorah_cancel_payment_endpoint_notice", error=str(exc2))

        # 3. Try /v2/UpdatePaymentStatus with Release (releases held/authorized invoice transactions)
        try:
            release_payload = {"Operation": "Release", "Key": val, "KeyType": "InvoiceId"}
            body = await self._post("v2/UpdatePaymentStatus", release_payload)
            if isinstance(body, dict) and body.get("IsSuccess"):
                return body
        except Exception as exc3:
            logger.info("myfatoorah_release_payment_endpoint_notice", error=str(exc3))
            body = {"IsSuccess": False, "Message": str(exc3)}

        return body

    # ── WebhookHandler ──────────────────────────────────────────────────
    @staticmethod
    def build_signature_string(payload: Mapping[str, Any]) -> str | None:
        event_code = (
            _dig(payload, "Event.Code")
            if isinstance(payload.get("Event"), Mapping)
            else (payload.get("EventCode") or payload.get("Event"))
        )
        data = payload.get("Data") or {}
        if isinstance(data, Mapping) and "Transaction" not in data:
            tx_list = [t for t in (data.get("InvoiceTransactions") or []) if isinstance(t, Mapping)]
            if tx_list:
                data = {**data, "Transaction": tx_list[0]}

        if str(event_code) in ("1", "TransactionsStatusChanged", "PAYMENT_STATUS_CHANGED"):
            fields = _PAYMENT_SIGNATURE_FIELDS
        elif str(event_code) in ("2", "RefundStatusChanged", "REFUND_STATUS_CHANGED"):
            fields = _REFUND_SIGNATURE_FIELDS
        else:
            return None
        return ",".join(f"{f}={_to_str(_dig(data, f))}" for f in fields)

    def compute_signature(self, signature_string: str) -> str:
        digest = hmac.new(
            self._settings.MYFATOORAH_WEBHOOK_SECRET.encode("utf-8"),
            signature_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def verify_signature(self, headers: Mapping[str, str], payload: dict[str, Any]) -> bool:
        if not self._settings.MYFATOORAH_WEBHOOK_SECRET:
            logger.error("myfatoorah_webhook_secret_missing")
            return False
        provided = next((v for k, v in headers.items() if k.lower() == _SIGNATURE_HEADER), None)
        signature_string = self.build_signature_string(payload)
        if not provided or signature_string is None:
            return False
        expected = self.compute_signature(signature_string)
        return hmac.compare_digest(expected.encode("utf-8"), provided.strip().encode("utf-8"))

    def parse_event(self, payload: dict[str, Any]) -> GatewayWebhookEvent:
        code = str(
            _dig(payload, "Event.Code")
            if isinstance(payload.get("Event"), Mapping)
            else (payload.get("EventCode") or payload.get("Event") or "")
        )
        reference = _to_str(_dig(payload, "Event.Reference") or payload.get("WebhookReference")) or None
        data = payload.get("Data") or {}
        tx_list = [t for t in (data.get("InvoiceTransactions") or []) if isinstance(t, Mapping)]
        success_tx = next(
            (t for t in tx_list if _to_str(t.get("TransactionStatus")).strip().lower() in {"succss", "success", "succeeded"}),
            tx_list[0] if tx_list else {},
        )
        if isinstance(data, Mapping) and "Transaction" not in data and tx_list:
            data = {**data, "Transaction": success_tx}

        inv_id = _to_str(_dig(data, "Invoice.Id") or payload.get("InvoiceId") or payload.get("invoice_id")) or None
        pmt_id = _to_str(_dig(data, "Transaction.PaymentId") or success_tx.get("PaymentId") or payload.get("PaymentId") or payload.get("payment_id")) or None

        if code in ("1", "TransactionsStatusChanged", "PAYMENT_STATUS_CHANGED") or (inv_id or pmt_id):
            tx_status = _to_str(_dig(data, "Transaction.Status") or payload.get("TransactionStatus")).upper()
            inv_status = _to_str(_dig(data, "Invoice.Status") or payload.get("InvoiceStatus")).upper()
            if inv_status == "PAID" or tx_status == "SUCCESS" or (not inv_status and not tx_status):
                status = GatewayPaymentStatus.PAID
            elif tx_status in {"FAILED"} or inv_status in {"FAILED"}:
                status = GatewayPaymentStatus.FAILED
            elif tx_status in {"CANCELED", "CANCELLED"} or inv_status in {"CANCELED", "CANCELLED"}:
                status = GatewayPaymentStatus.CANCELED
            else:
                status = GatewayPaymentStatus.PENDING
            invoice_id = inv_id
            state = GatewayPaymentState(
                status=status,
                invoice_id=invoice_id,
                payment_id=pmt_id,
                transaction_id=_to_str(_dig(data, "Transaction.Id") or success_tx.get("TransactionId") or success_tx.get("Id") or payload.get("TransactionId")) or None,
                track_id=_to_str(_dig(data, "Transaction.TrackId") or success_tx.get("TrackId") or payload.get("TrackId")) or None,
                reference_id=_to_str(_dig(data, "Transaction.ReferenceId") or success_tx.get("ReferenceId") or payload.get("ReferenceId")) or None,
                payment_method=_to_str(_dig(data, "Transaction.PaymentMethod") or success_tx.get("PaymentMethod") or success_tx.get("PaymentGateway") or payload.get("PaymentMethod")) or None,
                amount=_to_decimal(_dig(data, "Amount.ValueInBaseCurrency") or payload.get("InvoiceValue")),
                currency=_to_str(_dig(data, "Amount.BaseCurrency") or success_tx.get("Currency") or payload.get("Currency")) or None,
                failure_reason=_to_str(_dig(data, "Transaction.Error.Message") or success_tx.get("Error")) or None,
                raw={},  # card details are intentionally not retained
            )
            return GatewayWebhookEvent(
                kind=WebhookEventKind.PAYMENT_STATUS,
                event_reference=reference,
                invoice_id=invoice_id,
                status=status,
                state=state,
            )

        if code == "2":
            return GatewayWebhookEvent(
                kind=WebhookEventKind.REFUND_STATUS,
                event_reference=reference,
                invoice_id=_to_str(_dig(data, "ReferencedInvoice.Id")) or _to_str(_dig(data, "Refund.InvoiceId")) or None,
                refund_id=_to_str(_dig(data, "Refund.Id")) or None,
                refund_status=_to_str(_dig(data, "Refund.Status")).upper() or None,
                refund_amount=_to_decimal(_dig(data, "Amount.ValueInBaseCurrency")),
            )

        return GatewayWebhookEvent(kind=WebhookEventKind.IGNORED, event_reference=reference)


register_gateway(MyFatoorahGateway.name, lambda http, settings: MyFatoorahGateway(http, settings))
register_gateway("knet", lambda http, settings: MyFatoorahGateway(http, settings))
