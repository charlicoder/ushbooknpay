"""
app/payment_links/infrastructure/deema_gateway.py
──────────────────────────────────────────────────
Deema (via Tap Payments Charges API) adapter for :class:`PaymentLinkGateway`.

Docs: https://developers.tap.company/

* Link creation  : POST /v2/charges       (source.id = "src_deema", merchant.id from DEEMA_MERCHANT_ID)
* Status check   : GET  /v2/charges/{id}
* Refund         : POST /v2/refunds
* Webhook        : header ``hashstring`` = hex(HMAC-SHA256(secret_key,
                   "x_id{id}x_amount{amount}x_currency{currency}x_gateway_reference{gw_ref}"
                   "x_payment_reference{pmt_ref}x_status{status}x_created{created}"))

SECURITY: the secret key is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

import httpx

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
from app.payment_links.infrastructure.myfatoorah_gateway import (
    sanitize_customer_name,
    split_phone,
)

logger = get_logger(__name__)

_SIGNATURE_HEADERS = ("hashstring", "x-tap-signature", "tap-signature", "hash")
_THREE_DECIMAL_CURRENCIES = frozenset({"KWD", "BHD", "OMR", "JOD", "TND"})
_TAP_VALID_REFUND_REASONS = frozenset({"duplicate", "fraudulent", "requested_by_customer"})

_PAID_STATUSES = frozenset({"CAPTURED", "AUTHORIZED", "PAID", "SUCCESS", "SUCCEEDED"})
_FAILED_STATUSES = frozenset({"FAILED", "DECLINED", "RESTRICTED", "INVALID", "CAPTURED_FAILED"})
_CANCELED_STATUSES = frozenset({"CANCELLED", "CANCELED", "VOID", "ABANDONED"})
_EXPIRED_STATUSES = frozenset({"TIMEDOUT", "EXPIRED"})
_REFUND_OK_STATUSES = frozenset({"INITIATED", "PENDING", "ACCEPTED", "APPROVED", "REFUNDED", "SUCCEEDED"})
_REFUND_FAIL_STATUSES = frozenset({"FAILED", "DECLINED", "CANCELLED", "CANCELED", "RESTRICTED"})


def _to_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _format_tap_amount(amount: Any, currency: str) -> str:
    dec = _to_decimal(amount)
    if dec is None:
        return _to_str(amount)
    decimals = 3 if (currency or "KWD").upper() in _THREE_DECIMAL_CURRENCIES else 2
    return f"{dec:.{decimals}f}"


def _split_customer_name(full_name: str | None) -> tuple[str, str]:
    cleaned = sanitize_customer_name(full_name, default="Customer")
    parts = [p for p in cleaned.split(" ") if p]
    if not parts:
        return "Customer", "Customer"
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0][:50], " ".join(parts[1:])[:50]


def _map_charge_status(tap_status: str) -> GatewayPaymentStatus:
    upper = (tap_status or "").strip().upper()
    if upper in _PAID_STATUSES:
        return GatewayPaymentStatus.PAID
    if upper in _EXPIRED_STATUSES:
        return GatewayPaymentStatus.EXPIRED
    if upper in _CANCELED_STATUSES:
        return GatewayPaymentStatus.CANCELED
    if upper in _FAILED_STATUSES:
        return GatewayPaymentStatus.FAILED
    return GatewayPaymentStatus.PENDING


class DeemaGateway(PaymentLinkGateway):
    """Deema BNPL payment link gateway powered by Tap Payments Charges API."""

    name = "deema"
    provider_label = "Deema"

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._settings = settings

    # ── Configuration helpers ───────────────────────────────────────────
    @property
    def _secret_key(self) -> str:
        return (
            getattr(self._settings, "DEEMA_SECRET_KEY", "")
            or getattr(self._settings, "TAP_SECRET_KEY", "")
        ).strip()

    @property
    def _merchant_id(self) -> str:
        return (
            getattr(self._settings, "DEEMA_MERCHANT_ID", "")
            or getattr(self._settings, "TAP_MERCHANT_ID", "")
        ).strip()

    @property
    def _base_url(self) -> str:
        return (
            getattr(self._settings, "DEEMA_BASE_URL", "")
            or getattr(self._settings, "TAP_BASE_URL", "")
            or "https://api.tap.company/v2"
        ).strip()

    @property
    def _source_id(self) -> str:
        return (getattr(self._settings, "DEEMA_SOURCE_ID", "") or "src_deema").strip()

    @property
    def _default_country_code(self) -> str:
        return (
            getattr(self._settings, "DEEMA_DEFAULT_COUNTRY_CODE", "")
            or getattr(self._settings, "MYFATOORAH_DEFAULT_COUNTRY_CODE", "+965")
            or "+965"
        ).strip()

    # ── HTTP plumbing ───────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._secret_key:
            raise PaymentProviderError(
                "Deema (Tap) is not configured (missing secret key).",
                code="DEEMA_NOT_CONFIGURED",
            )
        headers = self._headers()
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._http.request(
                    method,
                    self._url(path),
                    json=payload,
                    headers=headers,
                    timeout=30.0,
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.status_code >= 400:
                    if attempt == 0 and response.status_code >= 500:
                        await asyncio.sleep(0.5)
                        continue
                    err_msg = self._error_message(body) if isinstance(body, Mapping) and body else f"HTTP {response.status_code}"
                    logger.error(
                        "deema_http_error",
                        path=path,
                        status=response.status_code,
                        message=err_msg,
                    )
                    raise PaymentProviderError(
                        f"Deema API error ({response.status_code}): {err_msg}",
                        code="DEEMA_API_ERROR",
                    )
                return body if isinstance(body, dict) else {}
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    logger.error("deema_timeout", path=path)
                    raise PaymentProviderError(
                        "Deema API timed out.", code="DEEMA_TIMEOUT"
                    ) from exc
                logger.error("deema_network_error", path=path, error=type(exc).__name__)
                raise PaymentProviderError(
                    "Deema API is unreachable.", code="DEEMA_UNREACHABLE"
                ) from exc
            except PaymentProviderError:
                raise
        if last_exc:
            raise last_exc
        return {}

    @staticmethod
    def _error_message(body: Mapping[str, Any]) -> str:
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for err in errors:
                if isinstance(err, Mapping):
                    desc = err.get("description") or err.get("message") or err.get("code")
                    if desc:
                        parts.append(str(desc))
            if parts:
                return "; ".join(parts)
        resp = body.get("response")
        if isinstance(resp, Mapping) and resp.get("message"):
            return str(resp.get("message"))
        if body.get("message"):
            return str(body.get("message"))
        return "Unknown error"

    def _resolve_callback_url(self, request_callback: str | None) -> str:
        if request_callback:
            return request_callback
        for attr in ("DEEMA_CALLBACK_URL", "DEEMA_SUCCESS_URL", "TAP_CALLBACK_URL", "TAP_REDIRECT_URL"):
            val = getattr(self._settings, attr, "")
            if val:
                return str(val).strip()
        mf_cb = getattr(self._settings, "MYFATOORAH_CALLBACK_URL", "")
        if mf_cb and "/webhook/myfatoorah" in mf_cb:
            return mf_cb.replace("/webhook/myfatoorah", "/webhook/deema")
        return getattr(self._settings, "MYFATOORAH_SUCCESS_URL", "") or ""

    def _resolve_webhook_url(self) -> str:
        for attr in ("DEEMA_WEBHOOK_URL", "DEEMA_CALLBACK_URL", "TAP_CALLBACK_URL"):
            val = getattr(self._settings, attr, "")
            if val:
                return str(val).strip()
        mf_wh = getattr(self._settings, "MYFATOORAH_WEBHOOK_URL", "")
        if mf_wh and "/webhook/myfatoorah" in mf_wh:
            return mf_wh.replace("/webhook/myfatoorah", "/webhook/deema")
        return ""

    # ── PaymentLinkCreator ──────────────────────────────────────────────
    async def create_link(self, request: PaymentLinkRequest) -> PaymentLinkResult:
        dial_code, number = split_phone(request.customer_phone, self._default_country_code)
        country_code = dial_code.lstrip("+") or "965"
        first_name, last_name = _split_customer_name(request.customer_name)
        email = (request.customer_email or "").strip() or f"{number or 'customer'}@ushspa.com"
        currency = (request.currency or "KWD").upper()

        customer_payload: dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
        }
        if number:
            customer_payload["phone"] = {
                "country_code": country_code,
                "number": number,
            }

        payload: dict[str, Any] = {
            "amount": float(request.amount),
            "currency": currency,
            "customer_initiated": True,
            "threeDSecure": True,
            "save_card": False,
            "description": (request.description or "USH Spa")[:100],
            "reference": {
                "transaction": request.reference,
                "order": request.reference,
            },
            "receipt": {
                "email": False,
                "sms": False,
            },
            "customer": customer_payload,
            "source": {
                "id": self._source_id,
            },
            "metadata": {
                "payment_link_id": request.reference,
                **(dict(request.metadata) if request.metadata else {}),
            },
        }

        if self._merchant_id:
            payload["merchant"] = {"id": self._merchant_id}

        callback_url = self._resolve_callback_url(request.callback_url)
        if callback_url:
            payload["redirect"] = {"url": callback_url}

        webhook_url = self._resolve_webhook_url()
        if webhook_url:
            payload["post"] = {"url": webhook_url}

        body = await self._request("POST", "charges", payload)

        charge_id = _to_str(body.get("id"))
        tx_obj = body.get("transaction") if isinstance(body.get("transaction"), Mapping) else {}
        url = _to_str(tx_obj.get("url") or body.get("url"))
        status_str = _to_str(body.get("status")).upper()

        if status_str in _FAILED_STATUSES or not charge_id or not url:
            msg = self._error_message(body)
            logger.warning("deema_create_charge_rejected", charge_id=charge_id, status=status_str, message=msg)
            raise PaymentProviderError(
                f"Deema rejected the payment link: {msg}",
                code="DEEMA_SEND_PAYMENT_FAILED",
            )

        logger.info("deema_link_created", charge_id=charge_id, reference=request.reference)
        return PaymentLinkResult(
            gateway_invoice_id=charge_id,
            payment_url=url,
            raw_response=body,
            raw_request=payload,
        )

    # ── PaymentStatusReader ─────────────────────────────────────────────
    async def get_payment_state(self, invoice_id: str) -> GatewayPaymentState:
        charge_id = str(invoice_id).strip()
        body = await self._request("GET", f"charges/{charge_id}")
        if not body or not body.get("id"):
            raise PaymentProviderError(
                f"Deema status lookup failed: {self._error_message(body or {})}",
                code="DEEMA_STATUS_FAILED",
            )
        return self._state_from_charge(body, fallback_id=charge_id)

    @staticmethod
    def _state_from_charge(body: Mapping[str, Any], *, fallback_id: str | None = None) -> GatewayPaymentState:
        charge_id = _to_str(body.get("id")) or _to_str(fallback_id)
        status = _map_charge_status(_to_str(body.get("status")))

        ref_obj = body.get("reference") if isinstance(body.get("reference"), Mapping) else {}
        tx_obj = body.get("transaction") if isinstance(body.get("transaction"), Mapping) else {}
        receipt_obj = body.get("receipt") if isinstance(body.get("receipt"), Mapping) else {}
        source_obj = body.get("source") if isinstance(body.get("source"), Mapping) else {}
        resp_obj = body.get("response") if isinstance(body.get("response"), Mapping) else {}
        meta_obj = body.get("metadata") if isinstance(body.get("metadata"), Mapping) else {}

        payment_id = (
            _to_str(ref_obj.get("payment"))
            or _to_str(receipt_obj.get("id"))
            or charge_id
            or None
        )
        transaction_id = (
            _to_str(ref_obj.get("transaction"))
            or _to_str(tx_obj.get("authorization_id"))
            or charge_id
            or None
        )
        track_id = (
            _to_str(ref_obj.get("track"))
            or _to_str(ref_obj.get("acquirer"))
            or None
        )
        reference_id = (
            _to_str(meta_obj.get("payment_link_id"))
            or _to_str(ref_obj.get("order"))
            or _to_str(ref_obj.get("gateway"))
            or None
        )
        raw_method = _to_str(source_obj.get("payment_method") or source_obj.get("id")).lower()
        payment_method = "deema" if ("deema" in raw_method or not raw_method) else raw_method

        failure_reason = (
            _to_str(resp_obj.get("message")) or None
            if status == GatewayPaymentStatus.FAILED
            else None
        )

        return GatewayPaymentState(
            status=status,
            invoice_id=charge_id or None,
            payment_id=payment_id,
            transaction_id=transaction_id,
            track_id=track_id,
            reference_id=reference_id,
            payment_method=payment_method,
            amount=_to_decimal(body.get("amount")),
            currency=_to_str(body.get("currency")) or "KWD",
            failure_reason=failure_reason,
            raw=dict(body),
        )

    # ── PaymentRefunder ─────────────────────────────────────────────────
    async def refund_payment(self, request: RefundRequest) -> RefundResponse:
        raw_reason = (request.reason or "").strip().lower()
        tap_reason = raw_reason if raw_reason in _TAP_VALID_REFUND_REASONS else "requested_by_customer"
        currency = (request.currency or "KWD").upper()

        payload: dict[str, Any] = {
            "charge_id": request.provider_payment_id,
            "currency": currency,
            "reason": tap_reason,
            "description": (request.reason or "Refund")[:200],
            "metadata": {
                "reason": (request.reason or "Refund")[:200],
            },
        }
        if request.amount is not None:
            payload["amount"] = float(request.amount)

        webhook_url = self._resolve_webhook_url()
        if webhook_url:
            payload["post"] = {"url": webhook_url}

        body = await self._request("POST", "refunds", payload)
        refund_id = _to_str(body.get("id"))
        refund_status = _to_str(body.get("status")).upper()

        if not refund_id or (refund_status and refund_status in _REFUND_FAIL_STATUSES):
            raise RefundError(
                f"Deema refund failed: {self._error_message(body)}",
                code="DEEMA_REFUND_FAILED",
            )

        logger.info("deema_refund_requested", charge_id=request.provider_payment_id, refund_id=refund_id)
        return RefundResponse(
            is_successful=True,
            provider_refund_id=refund_id,
            failure_reason=None,
            raw_response=body,
        )

    # ── Cancel Payment Link / Charge ────────────────────────────────────
    async def cancel_link(self, gateway_invoice_id: str) -> dict[str, Any]:
        """Best-effort cancellation for an open Deema / Tap charge."""
        if not gateway_invoice_id:
            return {"success": False, "message": "Missing gateway charge ID"}
        val = str(gateway_invoice_id).strip()
        logger.info("deema_cancel_link_request", charge_id=val)
        try:
            body = await self._request("POST", f"charges/{val}/void", {})
            if isinstance(body, dict) and body.get("id"):
                return body
        except Exception as exc:
            logger.info("deema_void_charge_notice", charge_id=val, error=str(exc))
        return {"success": True, "id": val}

    # ── WebhookHandler ──────────────────────────────────────────────────
    @staticmethod
    def build_signature_string(payload: Mapping[str, Any], *, formatted_amount: bool = True) -> str | None:
        charge_id = _to_str(payload.get("id"))
        if not charge_id:
            return None
        currency = _to_str(payload.get("currency") or "KWD").upper()
        raw_amt = payload.get("amount")
        amount_str = _format_tap_amount(raw_amt, currency) if formatted_amount else _to_str(raw_amt)

        ref_obj = payload.get("reference") if isinstance(payload.get("reference"), Mapping) else {}
        gateway_ref = _to_str(ref_obj.get("gateway"))
        payment_ref = _to_str(ref_obj.get("payment"))
        status_str = _to_str(payload.get("status"))

        tx_obj = payload.get("transaction") if isinstance(payload.get("transaction"), Mapping) else {}
        created_str = _to_str(tx_obj.get("created") or payload.get("created"))

        return (
            f"x_id{charge_id}"
            f"x_amount{amount_str}"
            f"x_currency{currency}"
            f"x_gateway_reference{gateway_ref}"
            f"x_payment_reference{payment_ref}"
            f"x_status{status_str}"
            f"x_created{created_str}"
        )

    def compute_signature(self, signature_string: str) -> str:
        return hmac.new(
            self._secret_key.encode("utf-8"),
            signature_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_signature(self, headers: Mapping[str, str], payload: dict[str, Any]) -> bool:
        if not self._secret_key:
            logger.error("deema_webhook_secret_missing")
            return False
        provided = next(
            (v for k, v in headers.items() if k.lower() in _SIGNATURE_HEADERS and v),
            None,
        )
        if not provided:
            return False
        provided_clean = provided.strip().lower()

        for fmt in (True, False):
            sig_str = self.build_signature_string(payload, formatted_amount=fmt)
            if sig_str is None:
                continue
            expected = self.compute_signature(sig_str).lower()
            if hmac.compare_digest(expected.encode("utf-8"), provided_clean.encode("utf-8")):
                return True
        return False

    def parse_event(self, payload: dict[str, Any]) -> GatewayWebhookEvent:
        obj_type = _to_str(payload.get("object")).lower()
        entity_id = _to_str(payload.get("id"))
        status_upper = _to_str(payload.get("status")).upper()

        # 1. Refund webhook event
        if obj_type == "refund" or entity_id.startswith("ref_"):
            charge_obj = payload.get("charge") if isinstance(payload.get("charge"), Mapping) else {}
            charge_id = _to_str(payload.get("charge_id") or charge_obj.get("id")) or None
            if status_upper in _REFUND_OK_STATUSES:
                norm_refund_status = "REFUNDED"
            elif status_upper in _REFUND_FAIL_STATUSES:
                norm_refund_status = "CANCELED"
            else:
                norm_refund_status = status_upper or None

            event_ref = f"{entity_id}:{status_upper}" if entity_id and status_upper else (entity_id or None)
            return GatewayWebhookEvent(
                kind=WebhookEventKind.REFUND_STATUS,
                event_reference=event_ref,
                invoice_id=charge_id,
                refund_id=entity_id or None,
                refund_status=norm_refund_status,
                refund_amount=_to_decimal(payload.get("amount")),
            )

        # 2. Charge / Payment webhook event
        if obj_type in ("charge", "authorize", "") and (entity_id or status_upper):
            state = self._state_from_charge(payload)
            state_no_raw = GatewayPaymentState(
                status=state.status,
                invoice_id=state.invoice_id,
                payment_id=state.payment_id,
                transaction_id=state.transaction_id,
                track_id=state.track_id,
                reference_id=state.reference_id,
                payment_method=state.payment_method,
                amount=state.amount,
                currency=state.currency,
                failure_reason=state.failure_reason,
                raw={},  # card/customer details not retained in state
            )
            event_ref = f"{entity_id}:{status_upper}" if entity_id and status_upper else (entity_id or None)
            return GatewayWebhookEvent(
                kind=WebhookEventKind.PAYMENT_STATUS,
                event_reference=event_ref,
                invoice_id=state.invoice_id,
                status=state.status,
                state=state_no_raw,
            )

        return GatewayWebhookEvent(kind=WebhookEventKind.IGNORED, event_reference=entity_id or None)


register_gateway(DeemaGateway.name, lambda http, settings: DeemaGateway(http, settings))
register_gateway("deema_paymentlink", lambda http, settings: DeemaGateway(http, settings))
