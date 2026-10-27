"""
app/payment/application/refund_service.py
─────────────────────────────────────────
Application service managing both Automated (Payment Gateway) and Manual (Desk / Branch) refunds.

Guarantees accounting and financial immutability:
- Original booking, invoice, and payment records remain intact for audit.
- Generates sequential refund numbers (REF/YYYY/MM/{NNNNNN}).
- Creates auditable Refund records.
- Emits SQS event `booking.refund_completed` for downstream orchestration (ushanr, ushnotice).
"""

from __future__ import annotations

from app.common.utils import to_local_tz as _to_local_tz
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.contracts import BookingRefundCompletedEvent
from app.booking.infrastructure.models import Booking
from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.events.sqs_client import get_sqs_client
from app.payment.domain.gateway_protocol import RefundRequest
from app.payment.domain.value_objects import (
    PaymentTransactionStatus,
    RefundMethod,
    RefundStatus,
    RefundType,
)
from app.payment.infrastructure.models import Payment, PaymentStatusHistory, Refund
from app.payment.infrastructure.providers.myfatoorah_provider import MyFatoorahProvider
from app.payment.infrastructure.providers.tap_provider import TapProvider
from app.payment.infrastructure.repository import generate_refund_number
from app.payment.interfaces.schemas import (
    CreateManualRefundRequest,
    ProcessGatewayRefundRequest,
)

logger = get_logger(__name__)

_KUWAIT_TZ = ZoneInfo("Asia/Kuwait")

# Provider refund statuses that represent a *settled* refund (provider confirmation).
# Anything else (e.g. MyFatoorah MakeRefund acceptance, Tap INITIATED/PENDING) means
# the refund is still processing and waits for the webhook / staff confirmation.
_PROVIDER_FINAL_REFUND_STATUSES = frozenset({"REFUNDED", "SUCCEEDED", "COMPLETED", "SUCCESS"})


def _now_kuwait() -> datetime:
    return datetime.now(timezone.utc)


from app.payment.application.refund_lifecycle import RefundLifecycle, _dec  # noqa: E402


class RefundService:
    """Manages refund workflows across automated payment gateways and manual desk operations."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def _get_booking(self, booking_id: uuid.UUID) -> Booking:
        stmt = select(Booking).where(Booking.id == booking_id)
        result = await self._session.execute(stmt)
        booking = result.scalar_one_or_none()
        if not booking:
            raise NotFoundError(f"Booking {booking_id} not found.")
        return booking

    async def _get_payment_for_booking(self, booking: Booking) -> Payment | None:
        stmt = (
            select(Payment)
            .where(Payment.booking_id == booking.id)
            .order_by(
                (Payment.status.in_(("success", "partially_refunded", "refunded"))).desc(),
                Payment.created_at.desc(),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        try:
            payment = result.scalar_one_or_none()
        except Exception:
            payment = result.scalars().first()
        if payment:
            return payment

        # Fallback to booking.payment_id
        if getattr(booking, "payment_id", None):
            try:
                p_uuid = uuid.UUID(str(booking.payment_id))
                stmt = select(Payment).where(Payment.id == p_uuid)
                result = await self._session.execute(stmt)
                payment = result.scalar_one_or_none()
                if payment:
                    return payment
            except (ValueError, TypeError):
                stmt = select(Payment).where(Payment.payment_id == str(booking.payment_id))
                result = await self._session.execute(stmt)
                payment = result.scalar_one_or_none()
                if payment:
                    return payment

        return None

    def _resolve_gateway_provider(self, payment: Payment):
        prov = str(payment.payment_provider or payment.payment_gateway or "").strip().lower()
        if "fatoorah" in prov:
            # Provider-agnostic payment-link gateway (also implements refund_payment)
            from app.payment_links import get_gateway

            return get_gateway("myfatoorah", self._http, self._settings)
        if "deema" in prov:
            from app.payment_links import get_gateway

            return get_gateway("deema", self._http, self._settings)
        return TapProvider(http_client=self._http, settings=self._settings)

    async def _resolve_refund_to_execute(
        self,
        booking: Booking,
        payment: Payment | None,
        *,
        net_amount: Decimal | None,
        cancellation_fee: Decimal,
        refund_type: str,
        refund_method: str,
        reason: str | None,
        notes: str | None,
        actor_id: str,
        actor_data: dict[str, Any] | None,
    ) -> Refund:
        """
        Reuse the booking's open *requested* refund (e.g. the one created on cancellation)
        instead of creating a duplicate; otherwise request a new (partial) refund.
        """
        lifecycle = RefundLifecycle(self._session)
        open_refund = await lifecycle.find_open_refund(booking.id)
        if open_refund is not None:
            if open_refund.status == RefundStatus.PROCESSING.value:
                raise ValidationError(
                    f"Refund {open_refund.refund_number} is already being processed by the payment provider. "
                    "Wait for the provider confirmation."
                )
            if net_amount is not None and (
                abs(_dec(open_refund.refunded_amount) - net_amount) > Decimal("0.0005")
                or abs(_dec(open_refund.cancellation_fee) - cancellation_fee) > Decimal("0.0005")
            ):
                await lifecycle.update_requested_amounts(
                    open_refund, payment, refund_amount=net_amount, cancellation_fee=cancellation_fee
                )
            open_refund.refund_type = refund_type
            open_refund.refund_method = refund_method
            if reason:
                open_refund.reason = reason
            if notes:
                open_refund.notes = notes
            return open_refund

        if net_amount is None:
            if payment is None:
                raise ValidationError("No payment record found for this booking.")
            net_amount = max(
                Decimal("0.000"),
                Decimal(str(payment.total_amount)) - await lifecycle.committed_total(payment) - cancellation_fee,
            )
        refund, _ = await lifecycle.request(
            booking,
            payment=payment,
            refund_amount=net_amount,
            cancellation_fee=cancellation_fee,
            refund_type=refund_type,
            refund_method=refund_method,
            reason=reason,
            notes=notes,
            actor_id=actor_id,
            actor_data=actor_data,
        )
        return refund

    async def record_manual_refund(
        self,
        booking_id: uuid.UUID,
        req: CreateManualRefundRequest,
        current_user_id: str,
        current_user_data: dict[str, Any] | None = None,
    ) -> Refund:
        """
        Record a staff-assisted manual refund executed outside the automated payment gateway
        (e.g. cash, card at branch, or bank transfer).

        The staff member's confirmation that the money was handed back IS the provider
        confirmation for manual refunds, so the refund is completed immediately. If the
        booking already has an open requested refund (created on cancellation) it is
        completed instead of creating a duplicate.
        """
        booking = await self._get_booking(booking_id)
        payment = await self._get_payment_for_booking(booking)

        requested_amount = Decimal(str(req.refund_amount))
        cancellation_fee = Decimal(str(req.cancellation_fee or "0.000"))
        if requested_amount <= 0:
            raise ValidationError("Refund amount must be greater than zero.")
        if cancellation_fee < 0:
            raise ValidationError("Cancellation fee cannot be negative.")
        if cancellation_fee > requested_amount:
            raise ValidationError("Cancellation fee cannot exceed requested refund amount.")

        effective_user_id = str(req.change_by_user or current_user_id)
        effective_user_data = req.change_by_user_data or current_user_data or {}

        refund = await self._resolve_refund_to_execute(
            booking,
            payment,
            net_amount=requested_amount - cancellation_fee,
            cancellation_fee=cancellation_fee,
            refund_type=RefundType.MANUAL.value,
            refund_method=RefundMethod.normalise(req.refund_method).value,
            reason=req.reason or "Manual staff refund",
            notes=req.notes,
            actor_id=effective_user_id,
            actor_data=effective_user_data,
        )
        refund.customer_confirmation = req.customer_confirmation or refund.customer_confirmation
        refund.reference_number = req.reference_number or refund.reference_number
        refund.processed_by = effective_user_id
        refund.processed_by_data = effective_user_data
        refund.processed_at = _now_kuwait()

        await RefundLifecycle(self._session).complete(
            refund,
            source="ushdesk_manual_refund",
            provider_reference=req.reference_number,
            actor_id=effective_user_id,
            actor_data=effective_user_data,
            publish_event=False,
        )
        if req.notes:
            booking.internal_notes = (f"{booking.internal_notes or ''}\n[Refund {refund.refund_number}]: {req.notes}").strip()
        await self._session.flush()

        # Emit SQS event for downstream orchestration
        await self._publish_refund_event(booking, refund, payment)

        logger.info(
            "manual_refund_recorded",
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            booking_id=str(booking.id),
            amount=str(refund.refunded_amount),
            method=refund.refund_method,
        )
        return refund

    async def process_gateway_refund(
        self,
        booking_id: uuid.UUID,
        req: ProcessGatewayRefundRequest,
        current_user_id: str,
        current_user_data: dict[str, Any] | None = None,
    ) -> Refund:
        """
        Submit an automated refund to the payment gateway (Tap or MyFatoorah).

        The gateway's acceptance only moves the refund to ``processing``. It becomes
        ``completed`` when the provider confirms it (MyFatoorah ``REFUNDED`` webhook,
        or an immediate final status from the provider response).
        """
        booking = await self._get_booking(booking_id)
        payment = await self._get_payment_for_booking(booking)
        if not payment:
            raise ValidationError("No payment record found for this booking to process a gateway refund.")

        cancellation_fee = Decimal(str(req.cancellation_fee or "0.000"))
        if cancellation_fee < 0:
            raise ValidationError("Invalid cancellation fee.")
        net_amount: Decimal | None = None
        if req.refund_amount is not None:
            requested_amount = Decimal(str(req.refund_amount))
            if requested_amount <= 0:
                raise ValidationError("Refund amount must be greater than zero.")
            if cancellation_fee > requested_amount:
                raise ValidationError("Invalid cancellation fee.")
            net_amount = requested_amount - cancellation_fee
            if net_amount <= 0:
                raise ValidationError("Net refunded amount after cancellation fee must be greater than zero.")

        # Identify external gateway transaction id
        prov_lower = str(payment.payment_provider or payment.payment_gateway or "").lower()
        if "fatoorah" in prov_lower or "deema" in prov_lower:
            gateway_payment_id = payment.invoice_id or payment.payment_id or payment.transaction_id
        else:
            gateway_payment_id = payment.payment_id or payment.transaction_id or payment.invoice_id
        if not gateway_payment_id:
            raise ValidationError("Payment lacks external gateway reference required for automated refund.")

        effective_user_id = str(req.change_by_user or current_user_id)
        effective_user_data = req.change_by_user_data or current_user_data or {}

        refund = await self._resolve_refund_to_execute(
            booking,
            payment,
            net_amount=net_amount,
            cancellation_fee=cancellation_fee,
            refund_type=RefundType.AUTOMATED.value,
            refund_method=RefundMethod.PAYMENT_GATEWAY.value,
            reason=req.reason or "Booking cancellation refund",
            notes=req.notes,
            actor_id=effective_user_id,
            actor_data=effective_user_data,
        )
        refund.payment_gateway = payment.payment_gateway or payment.payment_provider
        refund.gateway_transaction_id = payment.transaction_id

        provider = self._resolve_gateway_provider(payment)
        gateway_req = RefundRequest(
            provider_payment_id=gateway_payment_id,
            amount=Decimal(str(refund.refunded_amount)),
            reason=refund.reason or "Booking cancellation refund",
        )

        try:
            gateway_res = await provider.refund_payment(gateway_req)
        except Exception as exc:
            logger.error(
                "gateway_refund_failed_exception",
                booking_id=str(booking.id),
                payment_id=str(payment.id),
                refund_number=refund.refund_number,
                error=str(exc),
            )
            # Refund stays `requested` (nothing left the account) so staff can retry.
            raise ValidationError(f"Payment gateway refund failed: {exc}")

        if not gateway_res.is_successful:
            logger.warning(
                "gateway_refund_rejected",
                booking_id=str(booking.id),
                payment_id=str(payment.id),
                refund_number=refund.refund_number,
                reason=gateway_res.failure_reason,
            )
            raise ValidationError(f"Gateway refund declined: {gateway_res.failure_reason}")

        lifecycle = RefundLifecycle(self._session)
        await lifecycle.mark_processing(
            refund,
            provider_refund_id=gateway_res.provider_refund_id,
            gateway_response=gateway_res.raw_response,
            actor_id=effective_user_id,
            actor_data=effective_user_data,
        )

        # Gateway refund requests are asynchronous operations: the gateway accepts
        # the request for processing, and the refund record remains in `processing`
        # until confirmed via webhook or manual reconciliation.
        await self._session.flush()
        logger.info(
            "gateway_refund_submitted",
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            gateway_refund_id=refund.gateway_refund_id,
            status=refund.status,
            amount=str(refund.refunded_amount),
        )
        return refund

    async def confirm_refund(
        self,
        refund_id: uuid.UUID,
        *,
        provider_reference: str | None,
        notes: str | None,
        actor_id: str,
        actor_data: dict[str, Any] | None = None,
    ) -> Refund:
        """
        Staff confirmation that a ``processing`` refund was settled by the provider
        (fallback when the provider webhook is unavailable). Idempotent.
        """
        refund = (
            await self._session.execute(select(Refund).where(Refund.id == refund_id).with_for_update())
        ).scalar_one_or_none()
        if not refund:
            raise NotFoundError(f"Refund {refund_id} not found.")
        if notes:
            refund.notes = (f"{refund.notes or ''}\n{notes}").strip()
        await RefundLifecycle(self._session).complete(
            refund,
            source="ushdesk_refund_confirmation",
            provider_reference=provider_reference,
            actor_id=actor_id,
            actor_data=actor_data,
        )
        await self._session.flush()
        return refund

    async def link_credit_note(
        self,
        refund_id: uuid.UUID | None,
        *,
        booking_id: uuid.UUID | None = None,
        refund_number: str | None = None,
        credit_note_number: str | None,
        credit_note_id: str | None = None,
    ) -> Refund | None:
        """Link the ushanr credit note to its refund (credit note → refund relationship). Idempotent."""
        stmt = select(Refund)
        if refund_id:
            stmt = stmt.where(Refund.id == refund_id)
        elif refund_number:
            stmt = stmt.where(Refund.refund_number == refund_number)
        elif booking_id:
            stmt = stmt.where(Refund.booking_id == booking_id).order_by(Refund.created_at.desc())
        else:
            raise ValidationError("refund_id, refund_number or booking_id is required.")
        refund = (await self._session.execute(stmt.limit(1).with_for_update())).scalars().first()
        if refund is None:
            return None
        if credit_note_number and refund.credit_note_number not in (None, "", credit_note_number):
            logger.warning(
                "refund_credit_note_link_conflict",
                refund_number=refund.refund_number,
                existing=refund.credit_note_number,
                incoming=credit_note_number,
            )
            return refund
        if credit_note_number:
            refund.credit_note_number = credit_note_number
        if credit_note_id:
            refund.credit_note_id = str(credit_note_id)
        await self._session.flush()
        return refund

    async def _publish_refund_event(
        self, booking: Booking, refund: Refund, payment: Payment | None
    ) -> None:
        c_dict = booking.customer_data or {}
        b_dict = booking.branch_data or {}
        s_dict = booking.service_data or {}

        c_name = str(
            c_dict.get("name")
            or c_dict.get("full_name")
            or f"{c_dict.get('first_name', '')} {c_dict.get('last_name', '')}".strip()
            or ""
        )
        c_phone = str(c_dict.get("phone_number") or c_dict.get("phone") or "")
        c_email = str(c_dict.get("email") or "")

        event = BookingRefundCompletedEvent(
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            booking_id=str(booking.id),
            booking_number=getattr(booking, "booking_number", None) or "",
            booking_reference=str(getattr(booking, "booking_number", None) or booking.id),
            payment_id=str(payment.id) if payment else "",
            customer_id=str(booking.customer_id) if booking.customer_id else "",
            customer_name=c_name,
            customer_phone=c_phone,
            customer_email=c_email,
            customer_language="en" if str(c_dict.get("language_preference") or "").strip().lower().startswith("en") else "ar",
            branch_id=str(booking.branch_id) if booking.branch_id else "",
            branch_name=str(b_dict.get("name") or ""),
            service_id=str(booking.service_id) if booking.service_id else "",
            service_name=str(s_dict.get("name") or ""),
            refund_type=refund.refund_type,
            refund_method=refund.refund_method,
            status=refund.status,
            requested_amount=str(refund.requested_amount),
            cancellation_fee=str(refund.cancellation_fee),
            refund_amount=str(refund.refunded_amount),
            currency=refund.currency,
            reason=refund.reason or "",
            notes=refund.notes or "",
            customer_confirmation=refund.customer_confirmation or "",
            reference_number=refund.reference_number or "",
            payment_gateway=refund.payment_gateway,
            gateway_refund_id=refund.gateway_refund_id,
            gateway_transaction_id=refund.gateway_transaction_id,
            processed_by=refund.processed_by or "",
            processed_by_data=refund.processed_by_data or {},
            processed_at=_to_local_tz(refund.processed_at).isoformat() if refund.processed_at else "",
            created_at=_to_local_tz(refund.created_at).isoformat() if refund.created_at else "",
        )

        try:
            sqs = get_sqs_client()
            import asyncio
            asyncio.create_task(sqs.publish_event(event))
            logger.info("refund_event_published", refund_number=refund.refund_number)
        except Exception as exc:
            logger.warning("refund_event_publish_failed", refund_number=refund.refund_number, error=str(exc))

    async def create_refund_request(
        self,
        *,
        booking_id: uuid.UUID,
        payment_id: str | None = None,
        invoice_id: str | None = None,
        invoice_number: str | None = None,
        refund_amount: Decimal | None = None,
        cancellation_fee: Decimal = Decimal("0.000"),
        reason: str | None = None,
        notes: str | None = None,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
    ) -> tuple[Refund, bool]:
        """Create a refund record in requested status on booking cancellation."""
        booking = await self._get_booking(booking_id)
        payment = None
        if payment_id:
            try:
                p_uuid = uuid.UUID(str(payment_id))
                stmt = select(Payment).where(Payment.id == p_uuid)
                payment = (await self._session.execute(stmt)).scalar_one_or_none()
            except (ValueError, TypeError):
                stmt = select(Payment).where(Payment.payment_id == str(payment_id))
                payment = (await self._session.execute(stmt)).scalar_one_or_none()
        if not payment:
            payment = await self._get_payment_for_booking(booking)

        tot_amt = Decimal(str((payment.total_amount if payment else None) or booking.total_amount or "0.000"))
        fee_dec = Decimal(str(cancellation_fee or "0.000"))
        if refund_amount is not None:
            net_amt = Decimal(str(refund_amount))
        else:
            net_amt = max(Decimal("0.000"), tot_amt - fee_dec)

        from app.payment.application.refund_lifecycle import cancellation_idempotency_key
        lifecycle = RefundLifecycle(self._session)
        refund, created = await lifecycle.request(
            booking,
            payment=payment,
            refund_amount=net_amt,
            cancellation_fee=fee_dec,
            refund_type=RefundType.MANUAL.value,
            refund_method=RefundMethod.OTHER.value,
            reason=reason or "Booking cancellation refund requested",
            notes=notes or f"Refund requested on cancellation of booking {booking.booking_number or booking.id}",
            idempotency_key=cancellation_idempotency_key(booking.id),
            actor_id=actor_id,
            actor_data=actor_data,
        )
        if invoice_number and not refund.invoice_number:
            refund.invoice_number = invoice_number
        if invoice_id and not getattr(refund, "credit_note_id", None):
            refund.credit_note_id = str(invoice_id)
        if payment and not refund.payment_id:
            refund.payment_id = payment.id
        await self._session.flush()
        return refund, created

    async def approve_refund(
        self,
        refund_id: uuid.UUID,
        *,
        refund_method: str,
        approved_amount: Decimal,
        notes: str | None = None,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
    ) -> Refund:
        """Approve a requested refund with refund method (money or loyalty_points) and approved amount."""
        refund = await self.get_refund_by_id(refund_id)
        lifecycle = RefundLifecycle(self._session)
        refund = await lifecycle.approve(
            refund,
            refund_method=refund_method,
            approved_amount=approved_amount,
            notes=notes,
            actor_id=actor_id,
            actor_data=actor_data,
        )
        await self._session.flush()

        # Publish BookingRefundApprovedEvent
        try:
            booking = await self._get_booking(refund.booking_id) if refund.booking_id else None
            payment = await self._get_payment_for_booking(booking) if booking else None
            from app.events.contracts import BookingRefundApprovedEvent
            event = BookingRefundApprovedEvent(
                refund_id=str(refund.id),
                refund_number=refund.refund_number,
                booking_id=str(refund.booking_id or ""),
                booking_number=getattr(booking, "booking_number", None) or "",
                booking_reference=str(getattr(booking, "booking_number", None) or (refund.booking_id or "")),
                payment_id=str(refund.payment_id or (payment.id if payment else "")),
                customer_id=str(refund.customer_id or ""),
                branch_id=str(refund.branch_id or ""),
                refund_type=refund.refund_type,
                refund_method=refund.refund_method,
                status=refund.status,
                requested_amount=str(refund.requested_amount),
                cancellation_fee=str(refund.cancellation_fee),
                refund_amount=str(refund.refunded_amount),
                approved_amount=str(refund.refunded_amount),
                currency=refund.currency,
                reason=refund.reason or "",
                notes=refund.notes or "",
                processed_by=refund.processed_by or "",
                processed_by_data=refund.processed_by_data or {},
                processed_at=_to_local_tz(refund.processed_at).isoformat() if refund.processed_at else "",
                created_at=_to_local_tz(refund.created_at).isoformat() if refund.created_at else "",
                invoice_number=refund.invoice_number or getattr(payment, "invoice_number", "") or (booking.invoice_number if booking else ""),
                invoice_id=getattr(refund, "credit_note_id", "") or "",
            )
            sqs = get_sqs_client()
            import asyncio
            asyncio.create_task(sqs.publish_event(event))
            logger.info("refund_approved_event_published", refund_number=refund.refund_number)
        except Exception as exc:
            logger.warning("refund_approved_event_publish_failed", refund_number=refund.refund_number, error=str(exc))

        return refund

    async def reject_refund(
        self,
        refund_id: uuid.UUID,
        *,
        reason: str | None = None,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
    ) -> Refund:
        """Reject a requested refund."""
        refund = await self.get_refund_by_id(refund_id)
        lifecycle = RefundLifecycle(self._session)
        refund = await lifecycle.reject(
            refund,
            reason=reason,
            actor_id=actor_id,
            actor_data=actor_data,
        )
        await self._session.flush()
        return refund

    async def process_refund(
        self,
        refund_id: uuid.UUID,
        *,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
    ) -> Refund:
        """Process an approved refund (via payment provider for money or loyalty adjustment)."""
        refund = await self.get_refund_by_id(refund_id)
        if refund.status != RefundStatus.APPROVED.value:
            raise ValidationError(f"Refund {refund.refund_number} is not in approved status ({refund.status}).")

        booking = await self._get_booking(refund.booking_id) if refund.booking_id else None
        payment = await self._get_payment_for_booking(booking) if booking else None
        effective_user_id = str(actor_id or refund.processed_by or "staff")
        effective_user_data = actor_data or refund.processed_by_data or {}

        lifecycle = RefundLifecycle(self._session)

        # ── Case A: Loyalty Points ─────────────────────────────────────
        if refund.refund_method == RefundMethod.LOYALTY_POINTS.value or str(refund.refund_method).lower() in ("loyalty_points", "loyalty", "points"):
            from app.loyalty.application.services import LoyaltyService
            loyalty_svc = LoyaltyService(self._session)
            pts = int(Decimal(str(refund.refunded_amount or 0)))
            if pts > 0 and refund.customer_id:
                try:
                    await loyalty_svc.adjust_points(
                        customer_id=refund.customer_id,
                        delta=pts,
                        description=f"Refund {refund.refund_number} approved as loyalty points",
                        created_by=effective_user_id,
                    )
                except Exception as exc:
                    logger.error("loyalty_credit_on_refund_process_failed", refund_number=refund.refund_number, error=str(exc))
                    raise ValidationError(f"Failed to credit loyalty points: {exc}")

            refund, _ = await lifecycle.complete(
                refund,
                source="loyalty_point_refund",
                actor_id=effective_user_id,
                actor_data=effective_user_data,
                publish_event=True,
            )
            if booking:
                booking.status = "cancelled"
                booking.payment_status = "refunded_with_points"
                await self._session.flush()

            logger.info("loyalty_refund_processed", refund_number=refund.refund_number, points=pts)
            return refund

        # ── Case B: Money (Provider Gateway / Counter) ───────────────────
        # Determine gateway/provider
        if payment and (payment.payment_gateway or payment.payment_provider):
            gateway_name = str(payment.payment_gateway or payment.payment_provider).lower()
        elif booking and (booking.payment_gateway or booking.payment_provider):
            gateway_name = str(booking.payment_gateway or booking.payment_provider).lower()
        else:
            gateway_name = "manual"

        provider_ref = None
        gateway_raw_res = None

        if payment and (
            "fatoorah" in gateway_name
            or "deema" in gateway_name
            or "tap" in gateway_name
            or "gateway" in str(refund.refund_method).lower()
            or "payment_link" in str(refund.refund_method).lower()
        ):
            if "fatoorah" in gateway_name or "deema" in gateway_name:
                gateway_payment_id = payment.invoice_id or payment.payment_id or payment.transaction_id
            else:
                gateway_payment_id = payment.payment_id or payment.transaction_id or payment.invoice_id

            if gateway_payment_id:
                provider = self._resolve_gateway_provider(payment)
                gateway_req = RefundRequest(
                    provider_payment_id=str(gateway_payment_id),
                    amount=Decimal(str(refund.refunded_amount)),
                    reason=refund.reason or "Booking cancellation refund",
                )
                try:
                    gateway_res = await provider.refund_payment(gateway_req)
                    if not gateway_res.is_successful:
                        raise ValidationError(f"Gateway refund declined: {gateway_res.failure_reason}")
                    provider_ref = gateway_res.provider_refund_id
                    gateway_raw_res = gateway_res.raw_response
                except Exception as exc:
                    logger.error("gateway_refund_failed", refund_number=refund.refund_number, error=str(exc))
                    raise ValidationError(f"Payment provider refund failed: {exc}")

        # Complete refund in lifecycle
        refund, _ = await lifecycle.complete(
            refund,
            source="money_refund_process",
            provider_reference=provider_ref,
            gateway_response=gateway_raw_res,
            actor_id=effective_user_id,
            actor_data=effective_user_data,
            publish_event=True,
        )

        # Update credit note status in ushanr (record outbound refund payment)
        if booking:
            try:
                from app.integrations.ushanr_client import UshanrClient
                ushanr = UshanrClient()
                await ushanr.create_credit_note(
                    source_document_type="booking",
                    source_document_id=str(booking.id),
                    refund_amount=refund.refunded_amount,
                    cancellation_fee=refund.cancellation_fee,
                    refund_method=refund.refund_method,
                    refund_number=refund.refund_number,
                    branch_id=str(booking.branch_id) if booking.branch_id else None,
                    processed_by=effective_user_id,
                    record_refund_payment=True,
                    notes=f"Processed refund {refund.refund_number} for booking {booking.booking_number or booking.id}",
                )
                await ushanr.aclose()
            except Exception as u_exc:
                logger.warning("credit_note_status_update_failed", refund_number=refund.refund_number, error=str(u_exc))

        await self._session.flush()
        logger.info("money_refund_processed", refund_number=refund.refund_number, amount=str(refund.refunded_amount))
        return refund

    async def get_booking_refunds(self, booking_id: uuid.UUID) -> list[Refund]:
        stmt = select(Refund).where(Refund.booking_id == booking_id).order_by(Refund.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_refund_by_id(self, refund_id: uuid.UUID) -> Refund:
        stmt = select(Refund).where(Refund.id == refund_id)
        result = await self._session.execute(stmt)
        refund = result.scalar_one_or_none()
        if not refund:
            raise NotFoundError(f"Refund {refund_id} not found.")
        return refund

    async def list_refunds(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        booking_id: uuid.UUID | str | None = None,
        payment_id: uuid.UUID | str | None = None,
        customer_id: uuid.UUID | str | None = None,
        branch_id: uuid.UUID | str | None = None,
        refund_type: str | None = None,
        refund_method: str | None = None,
        status: str | None = None,
        refund_number: str | None = None,
        invoice_number: str | None = None,
        credit_note_number: str | None = None,
        payment_gateway: str | None = None,
        gateway_refund_id: str | None = None,
        gateway_transaction_id: str | None = None,
        reference_number: str | None = None,
        processed_by: str | None = None,
        from_date: datetime | str | None = None,
        to_date: datetime | str | None = None,
        search: str | None = None,
        currency: str | None = None,
    ) -> tuple[list[Refund], int, dict[str, Any]]:
        """
        List all refunds with pagination, multi-attribute filtering, and aggregate analytics.
        """
        conditions = []

        if booking_id:
            try:
                b_uuid = uuid.UUID(str(booking_id))
                conditions.append(Refund.booking_id == b_uuid)
            except (ValueError, TypeError):
                pass

        if payment_id:
            try:
                p_uuid = uuid.UUID(str(payment_id))
                conditions.append(Refund.payment_id == p_uuid)
            except (ValueError, TypeError):
                pass

        if customer_id:
            try:
                c_uuid = uuid.UUID(str(customer_id))
                conditions.append(Refund.customer_id == c_uuid)
            except (ValueError, TypeError):
                pass

        if branch_id:
            try:
                br_uuid = uuid.UUID(str(branch_id))
                conditions.append(Refund.branch_id == br_uuid)
            except (ValueError, TypeError):
                pass

        if refund_type and isinstance(refund_type, str):
            conditions.append(Refund.refund_type == refund_type.lower().strip())

        if refund_method and isinstance(refund_method, str):
            conditions.append(Refund.refund_method == refund_method.lower().strip())

        if status and isinstance(status, str):
            st = status.lower().strip()
            if st == "pending":
                conditions.append(or_(Refund.status == "pending", Refund.status.ilike("%pending%")))
            else:
                conditions.append(Refund.status == st)

        if refund_number and isinstance(refund_number, str):
            conditions.append(Refund.refund_number.ilike(f"%{refund_number.strip()}%"))

        if invoice_number and isinstance(invoice_number, str):
            conditions.append(Refund.invoice_number.ilike(f"%{invoice_number.strip()}%"))

        if credit_note_number and isinstance(credit_note_number, str):
            conditions.append(Refund.credit_note_number.ilike(f"%{credit_note_number.strip()}%"))

        if payment_gateway and isinstance(payment_gateway, str):
            conditions.append(Refund.payment_gateway.ilike(f"%{payment_gateway.strip()}%"))

        if gateway_refund_id and isinstance(gateway_refund_id, str):
            conditions.append(Refund.gateway_refund_id == gateway_refund_id.strip())

        if gateway_transaction_id and isinstance(gateway_transaction_id, str):
            conditions.append(Refund.gateway_transaction_id == gateway_transaction_id.strip())

        if reference_number and isinstance(reference_number, str):
            conditions.append(Refund.reference_number.ilike(f"%{reference_number.strip()}%"))

        if processed_by and isinstance(processed_by, str):
            conditions.append(Refund.processed_by == processed_by.strip())

        if currency and isinstance(currency, str):
            conditions.append(Refund.currency == currency.upper().strip())

        if from_date:
            try:
                dt_from = from_date if isinstance(from_date, datetime) else datetime.fromisoformat(str(from_date))
                conditions.append(Refund.created_at >= dt_from)
            except Exception:
                pass

        if to_date:
            try:
                dt_to = to_date if isinstance(to_date, datetime) else datetime.fromisoformat(str(to_date))
                conditions.append(Refund.created_at <= dt_to)
            except Exception:
                pass

        if search and isinstance(search, str):
            s = f"%{search.strip()}%"
            conditions.append(
                or_(
                    Refund.refund_number.ilike(s),
                    Refund.invoice_number.ilike(s),
                    Refund.credit_note_number.ilike(s),
                    Refund.reference_number.ilike(s),
                    Refund.gateway_refund_id.ilike(s),
                    Refund.gateway_transaction_id.ilike(s),
                    Refund.reason.ilike(s),
                    Refund.notes.ilike(s),
                    Refund.processed_by.ilike(s),
                )
            )

        # Count total
        count_stmt = select(func.count(Refund.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_count = (await self._session.execute(count_stmt)).scalar_one()

        # Aggregates
        sum_stmt = select(
            func.coalesce(func.sum(Refund.refunded_amount), Decimal("0.000")).label("total_refunded_amount"),
            func.coalesce(func.sum(Refund.cancellation_fee), Decimal("0.000")).label("total_cancellation_fee"),
            func.coalesce(func.sum(Refund.requested_amount), Decimal("0.000")).label("total_requested_amount"),
        )
        if conditions:
            sum_stmt = sum_stmt.where(and_(*conditions))
        agg_res = (await self._session.execute(sum_stmt)).one()

        analytics = {
            "total_refunded_amount": str(agg_res.total_refunded_amount),
            "total_cancellation_fee": str(agg_res.total_cancellation_fee),
            "total_requested_amount": str(agg_res.total_requested_amount),
            "total_count": total_count,
        }

        # Data query
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_page_size

        data_stmt = (
            select(Refund)
            .order_by(Refund.created_at.desc())
            .offset(offset)
            .limit(safe_page_size)
        )
        if conditions:
            data_stmt = data_stmt.where(and_(*conditions))

        result = await self._session.execute(data_stmt)
        refunds = list(result.scalars().all())

        return refunds, total_count, analytics
