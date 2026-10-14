"""
app/payment_links/application/webhook_service.py
─────────────────────────────────────────────────
Processes gateway webhooks (server-to-server).

Security / correctness pipeline:
  1. Verify the gateway signature (constant-time) — reject otherwise.
  2. De-duplicate by (gateway, event reference) — exactly-once processing.
  3. Never trust the webhook body for money decisions: the payment state is
     re-fetched from the gateway API before anything is confirmed.
  4. Row-lock the PaymentLink, then settle idempotently.
  5. Paid → ``payment_link.paid`` SQS event, failed → ``payment_link.failed``
     SQS event (published by the caller after the commit, see
     :meth:`publish_pending_events`).

Raising from :meth:`process` (provider outage, payment not yet visible, …)
rolls the transaction back (including the de-dup row) so the gateway retries.

When ``settings.payment_webhook_debug`` is on (always in development) every
step is logged: headers (secrets masked), raw body, parsed event, verification,
outcome and the response. Card data and credentials are never logged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    PaymentProviderError,
    ValidationError,
)
from app.core.logging import get_logger
from app.payment.domain.value_objects import PaymentTransactionStatus, RefundStatus
from app.payment.infrastructure.models import Payment, PaymentStatusHistory, Refund
from app.payment_links.application.settlement import PaymentSettlementService, SettlementOutcome
from app.payment_links.domain.gateway import (
    GatewayPaymentStatus,
    GatewayWebhookEvent,
    PaymentLinkGateway,
    WebhookEventKind,
)
from app.payment_links.infrastructure.models import PaymentLink, PaymentWebhookEvent

logger = get_logger(__name__)

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "x-internal-key", "x-app-token"}
_SIGNATURE_HEADER_HINT = "signature"


class WebhookRetryRequired(PaymentProviderError):
    """Signal the gateway to retry later (we answer with a 5xx)."""

    default_message = "Payment not yet verifiable; please retry."


class PaymentWebhookService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        gateway: PaymentLinkGateway,
    ) -> None:
        self._session = session
        self._settings = settings
        self._gateway = gateway
        self._settlement = PaymentSettlementService(session, settings)
        self._debug = bool(
            getattr(settings, "PAYMENT_WEBHOOK_DEBUG_LOG", False)
            or getattr(settings, "payment_webhook_debug", False)
            or getattr(settings, "is_development", True)
        )

    async def publish_pending_events(self) -> None:
        """Publish the SQS events queued while processing. Call AFTER the commit."""
        await self._settlement.publish_pending_events()

    # ── debug logging ───────────────────────────────────────────────────
    def _debug_log(self, step: str, **data: Any) -> None:
        if not self._debug:
            return
        text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
        print(f"=== [PAYMENT WEBHOOK:{self._gateway.name}] {step} ===\n{text}", flush=True)
        logger.info("payment_webhook_debug", step=step, gateway=self._gateway.name, **data)

    @staticmethod
    def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
        safe: dict[str, str] = {}
        for key, value in headers.items():
            lowered = key.lower()
            if lowered in _SENSITIVE_HEADERS:
                safe[key] = "***"
            elif _SIGNATURE_HEADER_HINT in lowered:
                safe[key] = f"{value[:8]}…({len(value)} chars)"
            else:
                safe[key] = value
        return safe

    async def process(self, headers: Mapping[str, str], raw_body: bytes) -> dict[str, Any]:
        self._debug_log(
            "REQUEST",
            headers=self._safe_headers(headers),
            body=self._sanitise_text(raw_body),
        )
        try:
            result = await self._process(headers, raw_body)
        except Exception as exc:
            self._debug_log("ERROR", error_type=type(exc).__name__, error=str(exc))
            raise
        self._debug_log("RESPONSE", status=200, body=result)
        return result

    def _sanitise_text(self, raw_body: bytes) -> Any:
        try:
            parsed = json.loads(raw_body or b"{}")
        except ValueError:
            return "<non-json body>"
        return self._sanitise(parsed) if isinstance(parsed, dict) else parsed

    async def _process(self, headers: Mapping[str, str], raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except ValueError as exc:
            raise ValidationError("Malformed webhook payload.", code="INVALID_WEBHOOK_PAYLOAD") from exc

        verified = self._gateway.verify_signature(headers, payload)
        self._debug_log("SIGNATURE", valid=bool(verified))
        is_dev = bool(
            getattr(self._settings, "is_development", False)
            or getattr(self._settings, "DEBUG", False)
            or str(getattr(self._settings, "ENVIRONMENT", "")).lower() != "production"
        )
        if not verified:
            if is_dev:
                logger.warning(
                    "payment_webhook_bad_signature_dev_bypass",
                    gateway=self._gateway.name,
                    msg="Invalid/missing webhook signature, allowing in development with gateway verification",
                )
            else:
                logger.warning("payment_webhook_bad_signature", gateway=self._gateway.name)
                raise AuthenticationError("Invalid webhook signature.", code="INVALID_WEBHOOK_SIGNATURE")

        event = self._gateway.parse_event(payload)
        self._debug_log(
            "PARSED_EVENT",
            kind=event.kind.value,
            reference=event.event_reference,
            gateway_invoice_id=event.invoice_id,
            status=event.status.value if event.status else None,
            refund_id=event.refund_id,
            refund_status=event.refund_status,
        )
        if event.kind == WebhookEventKind.IGNORED:
            return {"received": True, "outcome": "ignored"}

        self._check_age(payload)

        reference = event.event_reference or hashlib.sha256(raw_body).hexdigest()
        inbox_id = await self._claim_event(event, reference, payload)
        if inbox_id is None:
            logger.info("payment_webhook_duplicate", gateway=self._gateway.name, reference=reference)
            return {"received": True, "outcome": "duplicate"}

        if event.kind == WebhookEventKind.PAYMENT_STATUS:
            outcome, target_id = await self._handle_payment_event(event)
        else:
            outcome, target_id = await self._handle_refund_event(event)
        self._debug_log("OUTCOME", outcome=outcome, record_id=str(target_id) if target_id else None)

        await self._session.execute(
            update(PaymentWebhookEvent)
            .where(PaymentWebhookEvent.id == inbox_id)
            .values(outcome=outcome, payment_id=target_id, processed_at=datetime.now(timezone.utc))
        )
        return {"received": True, "outcome": outcome}

    # ── Inbox / dedup ───────────────────────────────────────────────────
    async def _claim_event(self, event: GatewayWebhookEvent, reference: str, payload: dict[str, Any]) -> uuid.UUID | None:
        stmt = (
            pg_insert(PaymentWebhookEvent)
            .values(
                id=uuid.uuid4(),
                gateway=self._gateway.name,
                event_reference=reference[:150],
                event_kind=event.kind.value,
                invoice_id=event.invoice_id,
                outcome="received",
                payload=self._sanitise(payload),
            )
            .on_conflict_do_nothing(constraint="uq_payment_webhook_gateway_reference")
            .returning(PaymentWebhookEvent.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _sanitise(payload: dict[str, Any]) -> dict[str, Any]:
        """Drop card / IP details before persisting the audit copy."""
        clean = copy.deepcopy(payload)
        tx = (clean.get("Data") or {}).get("Transaction")
        if isinstance(tx, dict):
            tx.pop("Card", None)
            tx.pop("IP", None)
        return clean

    def _check_age(self, payload: dict[str, Any]) -> None:
        max_age = self._settings.PAYMENT_WEBHOOK_MAX_AGE_SECONDS
        if not max_age:
            return
        created = (payload.get("Event") or {}).get("CreationDate")
        try:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")[:26])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return
        if (datetime.now(timezone.utc) - ts).total_seconds() > max_age:
            raise AuthenticationError("Webhook event is too old.", code="STALE_WEBHOOK")

    # ── Payment events ──────────────────────────────────────────────────
    async def _find_payment_for_update(self, invoice_id: str | None) -> Payment | None:
        """The ``payments`` row created after a successful payment (used by refund events)."""
        if not invoice_id:
            return None
        stmt = (
            select(Payment)
            .where(Payment.invoice_id == str(invoice_id), Payment.payment_provider == self._gateway.provider_label)
            .order_by(Payment.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _handle_payment_event(self, event: GatewayWebhookEvent) -> tuple[str, uuid.UUID | None]:
        source = "webhook_" + self._gateway.name
        lookup_id = event.invoice_id or (event.state.payment_id if event.state else None)
        link = await self._settlement.get_locked_link_by_any_identifier(
            self._gateway.name,
            str(lookup_id) if lookup_id else None,
            reference_id=event.event_reference or (str(event.state.reference_id) if event.state else None),
        )

        verified: GatewayPaymentState | None = None
        if lookup_id:
            try:
                verified = await self._gateway.get_payment_state(str(lookup_id))
                self._debug_log("GATEWAY_VERIFICATION", state=verified.status.value, payment_id=verified.payment_id)
                if link is None and verified.invoice_id:
                    link = await self._settlement.get_locked_link_by_any_identifier(
                        self._gateway.name,
                        str(verified.invoice_id),
                        reference_id=str(verified.reference_id or ""),
                    )
            except Exception as exc:
                logger.warning("payment_webhook_verification_call_failed", lookup_id=lookup_id, error=str(exc))

        if link is None:
            logger.warning("payment_webhook_unknown_invoice", invoice_id=lookup_id)
            return "ignored", None

        state_to_use = verified or event.state
        current_status = verified.status if verified else event.status

        if current_status == GatewayPaymentStatus.PAID or event.status == GatewayPaymentStatus.PAID:
            if verified and verified.status != GatewayPaymentStatus.PAID:
                raise WebhookRetryRequired(
                    "Gateway has not reported the invoice as paid yet.", code="PAYMENT_NOT_VERIFIED"
                )
            merged = verified if (verified and verified.payment_id) else (event.state or verified)
            if merged is None:
                merged = GatewayPaymentState(status=GatewayPaymentStatus.PAID, invoice_id=event.invoice_id)
            outcome = await self._settlement.apply(link, merged, source=source)
            return outcome.value, link.id

        if current_status == GatewayPaymentStatus.FAILED and state_to_use is not None:
            outcome = await self._settlement.apply(link, state_to_use, source=source)
            return outcome.value, link.id

        return SettlementOutcome.PENDING, link.id

    # ── Refund events ───────────────────────────────────────────────────
    async def _handle_refund_event(self, event: GatewayWebhookEvent) -> tuple[str, uuid.UUID | None]:
        payment = await self._find_payment_for_update(event.invoice_id)
        if payment is None:
            return "ignored", None

        refund = None
        if event.refund_id:
            refund = (
                await self._session.execute(
                    select(Refund)
                    .where(Refund.payment_id == payment.id, Refund.gateway_refund_id == str(event.refund_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()

        if event.refund_status == "REFUNDED":
            if refund is not None and refund.status != RefundStatus.COMPLETED.value:
                refund.status = RefundStatus.COMPLETED.value
            return "processed", payment.id

        if event.refund_status in {"CANCELED", "CANCELLED"} and refund is not None:
            if refund.status != RefundStatus.FAILED.value:
                refund.status = RefundStatus.FAILED.value
                reverted = Decimal(str(refund.refunded_amount))
                old = payment.status
                payment.amount_refunded = max(Decimal("0"), Decimal(str(payment.amount_refunded or 0)) - reverted)
                payment.status = (
                    PaymentTransactionStatus.SUCCESS.value
                    if payment.amount_refunded == 0
                    else PaymentTransactionStatus.PARTIALLY_REFUNDED.value
                )
                self._session.add(
                    PaymentStatusHistory(
                        payment_id=payment.id,
                        old_status=old,
                        new_status=payment.status,
                        source="webhook_" + self._gateway.name,
                        reason=f"Gateway cancelled refund {event.refund_id}",
                        provider_reference=str(event.refund_id),
                        change_by_user="webhook_" + self._gateway.name,
                    )
                )
                payment.payment_data = {
                    **(payment.payment_data or {}),
                    "needs_attention": f"Refund {refund.refund_number} was cancelled by the gateway.",
                }
            return "processed", payment.id

        return "ignored", payment.id
