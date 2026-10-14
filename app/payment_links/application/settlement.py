"""
app/payment_links/application/settlement.py
────────────────────────────────────────────
Applies a *verified* gateway payment outcome to our own data.

Single responsibility: given a :class:`PaymentLink` and the gateway-verified
:class:`GatewayPaymentState`, move the link / booking / gift voucher forward.
Used by the webhook processor and by the manual "sync status" endpoint, so the
rules live in exactly one place.

Rules (product spec):
  * PAID    → link=paid; booking: ``payment_status=success`` and a
              ``payment_link.paid`` SQS event is published — ushnotice then
              marks the invoice paid, creates the ``payments`` record and sends
              the SMS/WhatsApp. No ``payments`` row is created here for bookings.
              Gift voucher: a success ``payments`` row is created and the
              voucher is activated (existing voucher flow).
  * FAILED  → link=failed; booking: a ``payment_link.failed`` SQS event
              (ushnotice only logs it). Booking / voucher status is untouched and
              the link stays payable.
Idempotent: re-applying PAID to a settled link is a no-op.

SQS events are queued during settlement and published by
:meth:`publish_pending_events` **after** the DB transaction committed, so the
consumer can never observe stale data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.application.services import _build_booking_event_data
from app.booking.domain.value_objects import BookingStatus, PaymentStatus
from app.booking.infrastructure.models import Booking, BookingStatusHistory
from app.core.config import Settings
from app.core.exceptions import BookingStateError, ValidationError
from app.core.logging import get_logger
from app.events.contracts import BaseEvent, PaymentLinkFailedEvent, PaymentLinkPaidEvent
from app.payment.domain.value_objects import (
    PaymentFor,
    PaymentThrough,
    PaymentTransactionStatus,
)
from app.payment.infrastructure.models import Payment, PaymentStatusHistory
from app.payment.infrastructure.repository import generate_payment_number
from app.payment_links.domain.gateway import GatewayPaymentState, GatewayPaymentStatus
from app.payment_links.infrastructure.models import (
    PaymentLink,
    PaymentLinkStatus,
    PaymentLinkTarget,
)
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import GiftVoucherStatus
from app.voucher.infrastructure.models import GiftVoucher

logger = get_logger(__name__)

_SETTLED = {
    PaymentLinkStatus.PAID.value,
    PaymentLinkStatus.REFUNDED.value,
    PaymentLinkStatus.PARTIALLY_REFUNDED.value,
}
_AMOUNT_TOLERANCE = Decimal("0.001")
_NOT_PAYABLE_BOOKING = {BookingStatus.CANCELLED.value, BookingStatus.NO_SHOW.value}


class SettlementOutcome:
    SETTLED = "settled"
    ALREADY_SETTLED = "already_settled"
    FAILURE_RECORDED = "failure_recorded"
    PENDING = "pending"
    AMOUNT_MISMATCH = "amount_mismatch"


def _state_dict(state: GatewayPaymentState) -> dict[str, Any]:
    """Audit copy of a verified gateway state (no card / IP data)."""
    return {
        "status": state.status.value,
        "invoice_id": state.invoice_id,
        "payment_id": state.payment_id,
        "transaction_id": state.transaction_id,
        "track_id": state.track_id,
        "reference_id": state.reference_id,
        "payment_method": state.payment_method,
        "amount": str(state.amount) if state.amount is not None else None,
        "currency": state.currency,
        "failure_reason": state.failure_reason,
    }


class PaymentSettlementService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._pending_events: list[BaseEvent] = []

    # ── locking helpers ─────────────────────────────────────────────────
    async def get_locked_link(self, link_id: uuid.UUID) -> PaymentLink | None:
        stmt = select(PaymentLink).where(PaymentLink.id == link_id).with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_locked_link_by_gateway_invoice(self, gateway: str, gateway_invoice_id: str) -> PaymentLink | None:
        stmt = (
            select(PaymentLink)
            .where(PaymentLink.gateway == gateway, PaymentLink.gateway_invoice_id == str(gateway_invoice_id))
            .order_by(PaymentLink.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_locked_link_by_any_identifier(
        self,
        gateway: str,
        identifier: str | None,
        reference_id: str | None = None,
        payment_id: str | None = None,
    ) -> PaymentLink | None:
        """Find and lock a PaymentLink by gateway invoice id, gateway payment id, link UUID, or link number."""
        for key in (identifier, payment_id):
            if not key:
                continue
            link = await self.get_locked_link_by_gateway_invoice(gateway, str(key))
            if link:
                return link
            stmt_pmt = (
                select(PaymentLink)
                .where(PaymentLink.gateway == gateway, PaymentLink.gateway_payment_id == str(key))
                .order_by(PaymentLink.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            link_pmt = (await self._session.execute(stmt_pmt)).scalar_one_or_none()
            if link_pmt:
                return link_pmt

        for candidate in (reference_id, identifier, payment_id):
            if not candidate:
                continue
            try:
                parsed_uuid = uuid.UUID(str(candidate).strip())
                link_by_id = await self.get_locked_link(parsed_uuid)
                if link_by_id:
                    return link_by_id
            except (ValueError, AttributeError):
                pass
            stmt_num = (
                select(PaymentLink)
                .where(PaymentLink.link_number == str(candidate).strip())
                .limit(1)
                .with_for_update()
            )
            link_num = (await self._session.execute(stmt_num)).scalar_one_or_none()
            if link_num:
                return link_num

        return None

    # ── events ──────────────────────────────────────────────────────────
    async def publish_pending_events(self) -> None:
        """Publish queued events. Call only AFTER the transaction has been committed."""
        from app.events.sqs_client import get_sqs_client

        events, self._pending_events = self._pending_events, []
        if not events:
            return
        sqs = get_sqs_client()
        for event in events:
            try:
                await sqs.publish_event(event)
            except Exception as exc:  # never fail the webhook because of SQS
                logger.error(
                    "payment_link_event_publish_failed",
                    event_type=getattr(event, "event_type", None),
                    error=str(exc),
                )

    # ── entry point ─────────────────────────────────────────────────────
    async def apply(self, link: PaymentLink, state: GatewayPaymentState, *, source: str) -> str:
        if state.status == GatewayPaymentStatus.PAID:
            return await self._apply_paid(link, state, source=source)
        if state.status == GatewayPaymentStatus.FAILED:
            return await self._record_failure(link, state, source=source)
        if state.status in (GatewayPaymentStatus.EXPIRED, GatewayPaymentStatus.CANCELED):
            if link.status not in _SETTLED:
                link.status = (
                    PaymentLinkStatus.EXPIRED.value
                    if state.status == GatewayPaymentStatus.EXPIRED
                    else PaymentLinkStatus.CANCELLED.value
                )
            return SettlementOutcome.PENDING
        return SettlementOutcome.PENDING

    # ── paid ────────────────────────────────────────────────────────────
    async def _apply_paid(self, link: PaymentLink, state: GatewayPaymentState, *, source: str) -> str:
        if link.status in _SETTLED:
            return SettlementOutcome.ALREADY_SETTLED

        expected = Decimal(str(link.amount))
        if state.amount is not None and abs(state.amount - expected) > _AMOUNT_TOLERANCE:
            logger.error(
                "payment_link_amount_mismatch",
                link_id=str(link.id),
                expected=str(expected),
                received=str(state.amount),
            )
            link.meta = {
                **(link.meta or {}),
                "amount_mismatch": {"expected": str(expected), "received": str(state.amount)},
            }
            return SettlementOutcome.AMOUNT_MISMATCH

        now = datetime.now(timezone.utc)
        link.status = PaymentLinkStatus.PAID.value
        link.paid_at = now
        link.gateway_payment_id = state.payment_id or link.gateway_payment_id
        link.transaction_id = state.transaction_id or link.transaction_id
        link.track_id = state.track_id or link.track_id
        link.reference_id = state.reference_id or link.reference_id
        link.payment_method = state.payment_method or link.payment_method
        link.webhook_data = {**_state_dict(state), "settled_via": source, "settled_at": now.isoformat()}
        link.failure_data = None
        await self._session.flush()

        try:
            if link.target_type == PaymentLinkTarget.BOOKING.value:
                await self._settle_booking(link, now)
            elif link.target_type == PaymentLinkTarget.GIFT_VOUCHER.value:
                await self._settle_voucher(link, now)
        except (BookingStateError, ValidationError) as exc:
            # Customer paid but the booking/voucher can no longer be activated
            # (e.g. cancelled/expired meanwhile). Keep the money trail and flag
            # for staff — raising would make the gateway retry forever.
            logger.error("payment_received_but_entity_not_activatable", link_id=str(link.id), error=str(exc))
            link.meta = {
                **(link.meta or {}),
                "needs_attention": f"Payment received but target not activatable: {exc}",
            }

        await self._session.flush()
        return SettlementOutcome.SETTLED

    async def _settle_booking(self, link: PaymentLink, paid_at: datetime) -> None:
        booking = (
            await self._session.execute(select(Booking).where(Booking.id == link.target_id).with_for_update())
        ).scalar_one_or_none()
        if booking is None:
            raise ValidationError("Booking no longer exists.")
        if booking.status in _NOT_PAYABLE_BOOKING:
            raise BookingStateError(f"Booking is {booking.status}.")

        old_status = booking.status
        if booking.status in {
            BookingStatus.REQUESTED.value,
            BookingStatus.PAYMENT_PENDING.value,
            BookingStatus.DRAFT.value,
        }:
            booking.status = BookingStatus.CONFIRMED.value
            self._session.add(
                BookingStatusHistory(
                    id=uuid.uuid4(),
                    booking_id=booking.id,
                    old_status=old_status,
                    new_status=BookingStatus.CONFIRMED.value,
                    source=f"payment_link_{link.gateway}",
                    reason="Payment link settled via webhook/callback",
                )
            )
        booking.payment_status = PaymentStatus.SUCCESS.value
        booking.payment_id = link.gateway_payment_id or booking.payment_id
        booking.payment_provider = link.provider_label
        booking.payment_method = link.payment_method or booking.payment_method
        booking.payment_url = link.payment_url
        booking.payment_data = {
            **(booking.payment_data or {}),
            "payment_link_id": str(link.id),
            "gateway_invoice_id": link.gateway_invoice_id,
            "gateway_payment_id": link.gateway_payment_id,
            "transaction_id": link.transaction_id,
            "paid_at": paid_at.isoformat(),
        }
        if link.invoice_number and not booking.invoice_number:
            booking.invoice_number = link.invoice_number
        await self._session.flush()

        # Ensure Payment record exists in payments table for accounting & ushdesk
        payment_stmt = (
            select(Payment)
            .where(Payment.booking_id == booking.id)
            .order_by(Payment.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        payment = (await self._session.execute(payment_stmt)).scalar_one_or_none()
        if payment is None:
            payment_number = await generate_payment_number(self._session)
            payment = Payment(
                payment_number=payment_number,
                booking_id=booking.id,
                customer_id=booking.customer_id,
                customer_data=booking.customer_data,
                service_id=booking.service_id,
                service_data=booking.service_data,
                branch_id=booking.branch_id,
                branch_data=booking.branch_data,
                service_arrangement_id=booking.service_arrangement_id,
                service_arrangement_data=booking.service_arrangement_data,
                total_amount=link.amount,
                total_duration=int(booking.total_duration or booking.duration_minutes or 0),
                currency=link.currency,
                status=PaymentTransactionStatus.SUCCESS.value,
                payment_for=booking.booking_type or "branch_service",
                payment_provider=link.provider_label,
                payment_through=PaymentThrough.USHDESK.value,
                payment_gateway=link.gateway,
                payment_method=link.payment_method or "card",
                payment_url=link.payment_url,
                invoice_id=link.gateway_invoice_id,
                invoice_number=link.invoice_number or booking.invoice_number,
                invoice_value=link.amount,
                payment_id=link.gateway_payment_id,
                transaction_id=link.transaction_id,
                track_id=link.track_id,
                reference_id=link.reference_id,
                transaction_status="SUCCESS",
                transaction_date=paid_at.isoformat(),
                paid_at=paid_at,
                created_by_user=link.created_by,
                created_by_user_data=link.created_by_data,
                payment_data={"payment_link_id": str(link.id), "gateway": link.gateway},
            )
            self._session.add(payment)
            await self._session.flush()
            self._session.add(
                PaymentStatusHistory(
                    payment_id=payment.id,
                    old_status=None,
                    new_status=payment.status,
                    source="payment_link",
                    reason=f"{link.provider_label} payment confirmed",
                    provider_reference=link.gateway_payment_id or link.gateway_invoice_id,
                    correlation_id=str(link.id),
                    change_by_user=link.created_by or "payment_link",
                    change_by_user_data=link.created_by_data,
                )
            )
        else:
            payment.status = PaymentTransactionStatus.SUCCESS.value
            payment.payment_id = link.gateway_payment_id or payment.payment_id
            payment.transaction_id = link.transaction_id or payment.transaction_id
            payment.invoice_id = link.gateway_invoice_id or payment.invoice_id
            payment.transaction_status = "SUCCESS"
            payment.paid_at = paid_at
            payment.payment_provider = link.provider_label
            payment.payment_method = link.payment_method or payment.payment_method
            await self._session.flush()

        link.payment_row_id = payment.id

        self._pending_events.append(
            PaymentLinkPaidEvent(
                payment_link_id=str(link.id),
                target_type=link.target_type,
                target_id=str(link.target_id),
                booking_id=str(booking.id),
                invoice_id=link.invoice_id,
                invoice_number=link.invoice_number or booking.invoice_number,
                gateway=link.gateway,
                provider=link.provider_label,
                gateway_invoice_id=link.gateway_invoice_id or "",
                gateway_payment_id=link.gateway_payment_id or "",
                transaction_id=link.transaction_id or "",
                track_id=link.track_id or "",
                reference_id=link.reference_id or "",
                payment_method=link.payment_method or "",
                amount=str(link.amount),
                currency=link.currency,
                paid_at=paid_at.isoformat(),
                booking=_build_booking_event_data(booking),
            )
        )

    async def _settle_voucher(self, link: PaymentLink, paid_at: datetime) -> None:
        voucher = (
            await self._session.execute(select(GiftVoucher).where(GiftVoucher.id == link.target_id).with_for_update())
        ).scalar_one_or_none()
        if voucher is None:
            raise ValidationError("Gift voucher no longer exists.")

        payment_stmt = (
            select(Payment)
            .where(Payment.voucher_id == voucher.id)
            .order_by(Payment.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        payment = (await self._session.execute(payment_stmt)).scalar_one_or_none()
        if payment is None:
            payment = await self._create_voucher_payment(link, voucher, paid_at)
        else:
            payment.status = PaymentTransactionStatus.SUCCESS.value
            payment.paid_at = paid_at
            payment.payment_id = link.gateway_payment_id or payment.payment_id
            payment.transaction_id = link.transaction_id or payment.transaction_id
            payment.track_id = link.track_id or payment.track_id
            payment.reference_id = link.reference_id or payment.reference_id
            payment.payment_provider = link.provider_label or payment.payment_provider
            payment.payment_method = link.payment_method or payment.payment_method
            payment.payment_url = link.payment_url or payment.payment_url
            if link.invoice_number and not payment.invoice_number:
                payment.invoice_number = link.invoice_number
            if voucher.voucher_number and not payment.voucher_number:
                payment.voucher_number = voucher.voucher_number
            await self._session.flush()

        link.payment_row_id = payment.id
        snapshot = {
            "payment_id": link.gateway_payment_id or str(payment.id),
            "payment_number": payment.payment_number,
            "invoice_id": link.gateway_invoice_id or payment.invoice_id,
            "invoice_number": link.invoice_number or payment.invoice_number,
            "payment_link_id": str(link.id),
            "status": payment.status,
            "payment_provider": link.provider_label,
            "payment_method": link.payment_method,
            "payment_url": link.payment_url,
            "transaction_id": link.transaction_id,
            "track_id": link.track_id,
            "reference_id": link.reference_id,
            "paid_at": paid_at.isoformat(),
        }
        await GiftVoucherService(self._session).update_status(
            voucher.id,
            GiftVoucherStatus.ACTIVE.value,
            payment_id=str(link.gateway_payment_id or link.gateway_invoice_id or payment.id),
            payment_data=snapshot,
            payment_url=link.payment_url,
            payment_provider=link.provider_label,
            payment_through=PaymentThrough.USHDESK.value,
            actor_id=link.created_by,
        )

    async def _create_voucher_payment(self, link: PaymentLink, voucher: GiftVoucher, paid_at: datetime) -> Payment:
        payment = Payment(
            payment_number=await generate_payment_number(self._session),
            customer_id=voucher.sender_id,
            customer_data=voucher.sender_data,
            service_id=voucher.service_id,
            service_data=voucher.service_data,
            branch_id=voucher.branch_id,
            branch_data=voucher.branch_data,
            service_arrangement_id=voucher.service_arrangement_id,
            service_arrangement_data=voucher.service_arrangement_data,
            total_amount=link.amount,
            total_duration=int(voucher.total_duration or 0),
            currency=link.currency,
            status=PaymentTransactionStatus.SUCCESS.value,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            recipient_phone=voucher.recipient_phone,
            recipient_data=voucher.recipient_data,
            payment_method=link.payment_method or "payment_link",
            payment_through=PaymentThrough.USHDESK.value,
            payment_provider=link.provider_label,
            payment_for=PaymentFor.GIFT_VOUCHER.value,
            payment_url=link.payment_url,
            invoice_id=link.gateway_invoice_id,
            invoice_number=link.invoice_number,
            invoice_value=link.amount,
            payment_id=link.gateway_payment_id,
            transaction_id=link.transaction_id,
            track_id=link.track_id,
            reference_id=link.reference_id,
            transaction_status="SUCCESS",
            transaction_date=paid_at.isoformat(),
            paid_at=paid_at,
            created_by_user=link.created_by,
            created_by_user_data=link.created_by_data,
            payment_data={"payment_link_id": str(link.id), "gateway": link.gateway},
        )
        self._session.add(payment)
        await self._session.flush()
        self._session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=None,
                new_status=payment.status,
                source="payment_link",
                reason=f"{link.provider_label} payment confirmed",
                provider_reference=link.gateway_payment_id or link.gateway_invoice_id,
                correlation_id=str(link.id),
                change_by_user=link.created_by or "payment_link",
                change_by_user_data=link.created_by_data,
            )
        )
        return payment

    # ── failed ──────────────────────────────────────────────────────────
    async def _record_failure(self, link: PaymentLink, state: GatewayPaymentState, *, source: str) -> str:
        """Failed attempt: booking/voucher untouched, the link stays payable."""
        if link.status in _SETTLED:
            return SettlementOutcome.ALREADY_SETTLED
        now = datetime.now(timezone.utc)
        link.status = PaymentLinkStatus.FAILED.value
        link.payment_method = state.payment_method or link.payment_method
        link.failure_data = {
            "reason": state.failure_reason,
            "payment_method": state.payment_method,
            "source": source,
            "at": now.isoformat(),
        }
        link.webhook_data = {**_state_dict(state), "settled_via": source, "settled_at": now.isoformat()}

        if link.target_type == PaymentLinkTarget.BOOKING.value:
            self._pending_events.append(
                PaymentLinkFailedEvent(
                    payment_link_id=str(link.id),
                    target_type=link.target_type,
                    target_id=str(link.target_id),
                    booking_id=str(link.target_id),
                    invoice_id=link.invoice_id,
                    invoice_number=link.invoice_number,
                    gateway=link.gateway,
                    provider=link.provider_label,
                    gateway_invoice_id=link.gateway_invoice_id or "",
                    payment_method=state.payment_method or "",
                    amount=str(link.amount),
                    currency=link.currency,
                    failure_reason=state.failure_reason or "",
                )
            )
        return SettlementOutcome.FAILURE_RECORDED
