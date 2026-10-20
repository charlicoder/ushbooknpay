"""
app/payment/application/refund_lifecycle.py
────────────────────────────────────────────
Single authority for refund state transitions on booking payments.

Lifecycle (three distinct events, never collapsed):

    cancellation request ─► Refund(status=requested)
    refund request sent  ─► Refund(status=processing, gateway_refund_id=…)
    provider confirmed   ─► Refund(status=completed, completed_at=…)
                             └─► payment.amount_refunded recomputed
                             └─► booking.payment_status = partially_refunded | refunded
                             └─► SQS booking.refund_completed (exactly once per refund)

Accounting / audit guarantees
  * The original payment row is never deleted and its status is never overwritten
    (stays ``success``). ``amount_refunded`` is a derived summary recomputed from
    completed refunds, so retries can never double count.
  * Cumulative successful (+ open) refunds can never exceed the payment total.
  * Every transition is idempotent (idempotency_key on request, status guard on
    complete/fail) so retried webhooks / API calls / SQS redeliveries are safe.
  * Every transition writes an immutable ``payment_status_history`` audit row with
    actor, reason and provider reference.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.payment.domain.value_objects import (
    PaymentTransactionStatus,
    RefundMethod,
    RefundStatus,
    RefundType,
)
from app.payment.infrastructure.models import Payment, PaymentStatusHistory, Refund

logger = get_logger(__name__)

ZERO = Decimal("0.000")
TOLERANCE = Decimal("0.0005")

OPEN_STATUSES = (
    RefundStatus.REQUESTED.value,
    RefundStatus.APPROVED.value,
    RefundStatus.PROCESSING.value,
)
PAID_PAYMENT_STATUSES = ("success", "paid", "partially_refunded", "refunded", "refunded_with_points")


def cancellation_idempotency_key(booking_id: Any) -> str:
    return f"booking-cancel:{booking_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v if v is not None else "0")).quantize(Decimal("0.001"))
    except Exception:
        return ZERO


def _iso(dt: datetime | None) -> str:
    if not dt:
        return ""
    try:
        from app.common.utils import to_local_tz

        return to_local_tz(dt).isoformat()
    except Exception:
        return dt.isoformat()


class RefundLifecycle:
    """Refund state machine bound to one DB session (caller owns the commit)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Lookups ───────────────────────────────────────────────────────────

    async def find_payment_for_booking(self, booking: Any, *, for_update: bool = False) -> Payment | None:
        """Paid payment row for a booking (success-first, newest-first)."""
        stmt = (
            select(Payment)
            .where(Payment.booking_id == booking.id)
            .order_by(Payment.status.in_(PAID_PAYMENT_STATUSES).desc(), Payment.created_at.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        payment = (await self._session.execute(stmt)).scalars().first()
        if payment is not None or not getattr(booking, "payment_id", None):
            return payment
        raw = str(booking.payment_id)
        try:
            cond = Payment.id == uuid.UUID(raw)
        except (ValueError, TypeError):
            cond = Payment.payment_id == raw
        stmt = select(Payment).where(cond).limit(1)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalars().first()

    async def find_open_refund(self, booking_id: Any) -> Refund | None:
        """Oldest open (requested/processing) refund for a booking."""
        stmt = (
            select(Refund)
            .where(Refund.booking_id == booking_id, Refund.status.in_(OPEN_STATUSES))
            .order_by(Refund.created_at.asc())
            .limit(1)
            .with_for_update()
        )
        res = (await self._session.execute(stmt)).scalars().first()
        if not isinstance(res, Refund):
            return None
        return res

    async def _sum(self, payment_id: Any, statuses: tuple[str, ...], exclude_id: Any = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(Refund.refunded_amount), ZERO)).where(
            Refund.payment_id == payment_id, Refund.status.in_(statuses)
        )
        if exclude_id is not None:
            stmt = stmt.where(Refund.id != exclude_id)
        return _dec((await self._session.execute(stmt)).scalar_one())

    async def completed_total(self, payment: Payment, exclude_id: Any = None) -> Decimal:
        return await self._sum(payment.id, (RefundStatus.COMPLETED.value,), exclude_id)

    async def committed_total(self, payment: Payment, exclude_id: Any = None) -> Decimal:
        """Completed + open refunds — the amount already promised back to the customer."""
        return await self._sum(payment.id, (RefundStatus.COMPLETED.value, *OPEN_STATUSES), exclude_id)

    async def _retained_fee(self, payment: Payment) -> Decimal:
        stmt = select(func.coalesce(func.max(Refund.cancellation_fee), ZERO)).where(
            Refund.payment_id == payment.id,
            Refund.status.in_((RefundStatus.COMPLETED.value, *OPEN_STATUSES)),
        )
        return _dec((await self._session.execute(stmt)).scalar_one())

    async def refundable_target(self, payment: Payment) -> Decimal:
        """Amount eligible for refund = payment total − retained cancellation fee."""
        return max(ZERO, _dec(payment.total_amount) - await self._retained_fee(payment))

    async def is_fully_refunded(self, booking: Any) -> bool:
        payment = await self.find_payment_for_booking(booking)
        if payment is None:
            return False
        target = await self.refundable_target(payment)
        return target > ZERO and (await self.completed_total(payment)) + TOLERANCE >= target

    # ── Transitions ───────────────────────────────────────────────────────

    async def request(
        self,
        booking: Any,
        *,
        payment: Payment | None,
        refund_amount: Decimal,
        cancellation_fee: Decimal = ZERO,
        refund_type: str = RefundType.MANUAL.value,
        refund_method: str = RefundMethod.CASH.value,
        reason: str | None = None,
        notes: str | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        actor_data: dict | None = None,
    ) -> tuple[Refund, bool]:
        """
        Create a refund in ``requested`` state. Returns (refund, created).
        Idempotent per ``idempotency_key``. Enforces the cumulative refund cap.
        """
        if idempotency_key:
            existing = (
                await self._session.execute(select(Refund).where(Refund.idempotency_key == idempotency_key))
            ).scalars().first()
            if existing is not None:
                return existing, False

        net = _dec(refund_amount)
        fee = _dec(cancellation_fee)
        if net <= ZERO:
            raise ValidationError("Refund amount must be greater than zero.")
        if fee < ZERO:
            raise ValidationError("Cancellation fee cannot be negative.")

        if payment is not None:
            total = _dec(payment.total_amount)
            committed = await self.committed_total(payment)
            if committed + net > total + TOLERANCE:
                raise ValidationError(
                    f"Refund amount {net} exceeds available refundable balance ({max(ZERO, total - committed)} "
                    f"{payment.currency or 'KWD'})."
                )

        from app.payment.infrastructure.repository import generate_refund_number

        refund = Refund(
            id=uuid.uuid4(),
            refund_number=await generate_refund_number(self._session),
            idempotency_key=idempotency_key,
            booking_id=booking.id,
            booking_data=self._booking_snapshot(booking),
            payment_id=payment.id if payment else None,
            invoice_number=(getattr(payment, "invoice_number", None) if payment else None)
            or getattr(booking, "invoice_number", None),
            customer_id=booking.customer_id,
            customer_data=booking.customer_data,
            branch_id=booking.branch_id,
            branch_data=booking.branch_data,
            refund_type=refund_type,
            refund_method=refund_method,
            status=RefundStatus.REQUESTED.value,
            requested_amount=net + fee,
            cancellation_fee=fee,
            refunded_amount=net,
            currency=getattr(booking, "currency", None) or (payment.currency if payment else "KWD"),
            reason=reason or "Booking cancellation refund",
            notes=notes,
            payment_gateway=(payment.payment_gateway or payment.payment_provider) if payment else None,
            gateway_transaction_id=payment.transaction_id if payment else None,
            processed_by=str(actor_id) if actor_id else None,
            processed_by_data=actor_data or {},
            processed_at=_now(),
        )
        self._session.add(refund)
        await self._session.flush()
        if payment is not None:
            self._audit(
                payment,
                reason=f"Refund {refund.refund_number} requested: {net} {refund.currency}",
                refund=refund,
                actor_id=actor_id,
                actor_data=actor_data,
                source="refund_requested",
            )
        logger.info(
            "refund_requested",
            refund_number=refund.refund_number,
            booking_id=str(booking.id),
            amount=str(net),
            idempotency_key=idempotency_key,
        )
        return refund, True

    async def update_requested_amounts(
        self, refund: Refund, payment: Payment | None, *, refund_amount: Decimal, cancellation_fee: Decimal
    ) -> None:
        """Adjust an open (not yet sent) refund before execution. Enforces the cap."""
        if refund.status != RefundStatus.REQUESTED.value:
            raise ValidationError(f"Refund {refund.refund_number} can no longer be modified ({refund.status}).")
        net, fee = _dec(refund_amount), _dec(cancellation_fee)
        if net <= ZERO:
            raise ValidationError("Net refund amount must be greater than zero.")
        if payment is not None:
            committed = await self.committed_total(payment, exclude_id=refund.id)
            if committed + net > _dec(payment.total_amount) + TOLERANCE:
                raise ValidationError(
                    f"Refund amount {net} exceeds available refundable balance "
                    f"({max(ZERO, _dec(payment.total_amount) - committed)})."
                )
        refund.refunded_amount = net
        refund.cancellation_fee = fee
        refund.requested_amount = net + fee

    async def reject(
        self,
        refund: Refund,
        *,
        reason: str | None = None,
        actor_id: str | None = None,
        actor_data: dict | None = None,
    ) -> Refund:
        """Mark a requested refund as rejected."""
        if refund.status in (RefundStatus.COMPLETED.value, RefundStatus.REJECTED.value):
            return refund
        if refund.status != RefundStatus.REQUESTED.value:
            raise ValidationError(f"Refund {refund.refund_number} cannot be rejected ({refund.status}).")
        refund.status = RefundStatus.REJECTED.value
        refund.failure_reason = reason or "Refund rejected by staff"
        if actor_id:
            refund.processed_by = str(actor_id)
            refund.processed_by_data = actor_data or refund.processed_by_data
        refund.processed_at = _now()
        await self._session.flush()
        logger.info(
            "refund_rejected",
            refund_number=refund.refund_number,
            booking_id=str(refund.booking_id),
            reason=reason,
        )
        return refund

    async def approve(
        self,
        refund: Refund,
        *,
        refund_method: str,
        approved_amount: Decimal,
        notes: str | None = None,
        actor_id: str | None = None,
        actor_data: dict | None = None,
    ) -> Refund:
        """Mark a requested refund as approved with chosen refund method and amount."""
        if refund.status == RefundStatus.APPROVED.value:
            return refund
        if refund.status != RefundStatus.REQUESTED.value:
            raise ValidationError(f"Refund {refund.refund_number} cannot be approved ({refund.status}).")
        net = _dec(approved_amount)
        if net <= ZERO:
            raise ValidationError("Approved refund amount must be greater than zero.")
        norm_method = RefundMethod.normalise(refund_method).value
        payment = await self._payment_of(refund)
        if payment is not None and norm_method != RefundMethod.LOYALTY_POINTS.value:
            committed = await self.committed_total(payment, exclude_id=refund.id)
            if committed + net > _dec(payment.total_amount) + TOLERANCE:
                raise ValidationError(
                    f"Approved refund amount {net} exceeds available refundable balance "
                    f"({max(ZERO, _dec(payment.total_amount) - committed)})."
                )
        refund.status = RefundStatus.APPROVED.value
        refund.refund_method = norm_method
        refund.refunded_amount = net
        refund.requested_amount = net + _dec(refund.cancellation_fee)
        if notes:
            refund.notes = notes
        if actor_id:
            refund.processed_by = str(actor_id)
            refund.processed_by_data = actor_data or refund.processed_by_data
        refund.processed_at = _now()
        if norm_method == RefundMethod.LOYALTY_POINTS.value:
            booking = await self._booking_of(refund)
            if booking is not None:
                booking.payment_status = "refunded_with_points"
        await self._session.flush()
        logger.info(
            "refund_approved",
            refund_number=refund.refund_number,
            booking_id=str(refund.booking_id),
            method=refund.refund_method,
            amount=str(net),
        )
        return refund

    async def mark_processing(
        self,
        refund: Refund,
        *,
        provider_refund_id: str | None,
        gateway_response: dict | None = None,
        actor_id: str | None = None,
        actor_data: dict | None = None,
    ) -> Refund:
        """Refund request accepted by the provider — awaiting provider confirmation."""
        if refund.status in (RefundStatus.COMPLETED.value, RefundStatus.PROCESSING.value):
            return refund
        if refund.status not in (RefundStatus.REQUESTED.value, RefundStatus.APPROVED.value):
            raise ValidationError(f"Refund {refund.refund_number} cannot be processed ({refund.status}).")
        refund.status = RefundStatus.PROCESSING.value
        refund.refund_type = RefundType.AUTOMATED.value
        refund.refund_method = RefundMethod.PAYMENT_GATEWAY.value
        if provider_refund_id:
            refund.gateway_refund_id = str(provider_refund_id)
        if gateway_response is not None:
            refund.gateway_response = gateway_response
        if actor_id:
            refund.processed_by = str(actor_id)
            refund.processed_by_data = actor_data or refund.processed_by_data
        refund.processed_at = _now()
        payment = await self._payment_of(refund)
        if payment is not None:
            self._audit(
                payment,
                reason=f"Refund {refund.refund_number} sent to provider (ref {provider_refund_id or '-'})",
                refund=refund,
                actor_id=actor_id,
                actor_data=actor_data,
                source="refund_processing",
                provider_reference=provider_refund_id,
            )
        await self._session.flush()
        return refund

    async def complete(
        self,
        refund: Refund,
        *,
        source: str,
        provider_reference: str | None = None,
        gateway_response: dict | None = None,
        actor_id: str | None = None,
        actor_data: dict | None = None,
        publish_event: bool = True,
    ) -> tuple[Refund, bool]:
        """
        Mark a refund completed after provider (or staff) confirmation.
        Returns (refund, transitioned). Idempotent: a completed refund is returned untouched.
        """
        if refund.status == RefundStatus.COMPLETED.value:
            return refund, False
        if refund.status not in OPEN_STATUSES:
            raise ValidationError(f"Refund {refund.refund_number} cannot be completed ({refund.status}).")

        payment = await self._payment_of(refund, for_update=True)
        is_loyalty = str(refund.refund_method).lower() in (
            RefundMethod.LOYALTY_POINTS.value,
            "loyalty_points",
            "loyalty",
            "points",
        )
        if payment is not None and not is_loyalty:
            done = await self.completed_total(payment, exclude_id=refund.id)
            if done + _dec(refund.refunded_amount) > _dec(payment.total_amount) + TOLERANCE:
                raise ValidationError(
                    f"Completing refund {refund.refund_number} would exceed the paid amount "
                    f"({payment.total_amount} {payment.currency})."
                )

        refund.status = RefundStatus.COMPLETED.value
        refund.completed_at = _now()
        refund.failure_reason = None
        if provider_reference:
            if refund.refund_method == RefundMethod.PAYMENT_GATEWAY.value:
                refund.gateway_refund_id = refund.gateway_refund_id or str(provider_reference)
            else:
                refund.reference_number = refund.reference_number or str(provider_reference)
        if gateway_response is not None:
            refund.gateway_response = {**(refund.gateway_response or {}), "confirmation": gateway_response}
        await self._session.flush()

        booking = await self._booking_of(refund)
        fully = False
        if payment is not None and not is_loyalty:
            old_payment_status = payment.status
            db_total = await self.completed_total(payment)
            calc_total = _dec(payment.amount_refunded) + _dec(refund.refunded_amount)
            payment.amount_refunded = max(db_total, calc_total)
            fee = max(await self._retained_fee(payment), _dec(refund.cancellation_fee))
            booking_target = max(ZERO, _dec(payment.total_amount) - fee)
            fully = booking_target > ZERO and _dec(payment.amount_refunded) + TOLERANCE >= booking_target
            if _dec(payment.amount_refunded) + TOLERANCE >= _dec(payment.total_amount):
                payment.status = PaymentTransactionStatus.REFUNDED.value
            elif _dec(payment.amount_refunded) > ZERO:
                payment.status = PaymentTransactionStatus.PARTIALLY_REFUNDED.value
            self._audit(
                payment,
                reason=(
                    f"Refund {refund.refund_number} completed: {refund.refunded_amount} {refund.currency} "
                    f"(total refunded {payment.amount_refunded}/{booking_target})"
                ),
                refund=refund,
                actor_id=actor_id,
                actor_data=actor_data,
                source=source,
                provider_reference=provider_reference or refund.gateway_refund_id,
                old_status=old_payment_status,
            )
        elif payment is not None and is_loyalty:
            # Keep original payment as paid/success
            self._audit(
                payment,
                reason=f"Refund {refund.refund_number} completed as loyalty points ({refund.refunded_amount} pts)",
                refund=refund,
                actor_id=actor_id,
                actor_data=actor_data,
                source=source,
                old_status=payment.status,
            )
        if booking is not None:
            if is_loyalty:
                booking.payment_status = "refunded_with_points"
                booking.status = "cancelled"
            else:
                booking.payment_status = "refunded" if (fully or payment is None) else "partially_refunded"
                if str(booking.status).lower() != "cancelled":
                    booking.status = "refunded"
        await self._session.flush()

        logger.info(
            "refund_completed",
            refund_number=refund.refund_number,
            booking_id=str(refund.booking_id),
            amount=str(refund.refunded_amount),
            fully_refunded=fully,
            source=source,
        )
        if publish_event:
            await self.publish_completed_event(refund, booking, payment, fully)
        return refund, True

    async def fail(
        self,
        refund: Refund,
        *,
        reason: str,
        source: str,
        provider_reference: str | None = None,
        gateway_response: dict | None = None,
        actor_id: str | None = None,
    ) -> Refund:
        """Provider rejected / cancelled the refund. Reverses summaries if it had completed."""
        if refund.status in (RefundStatus.FAILED.value, RefundStatus.CANCELLED.value):
            return refund
        was_completed = refund.status == RefundStatus.COMPLETED.value
        refund.status = RefundStatus.FAILED.value
        refund.failure_reason = reason
        if gateway_response is not None:
            refund.gateway_response = {**(refund.gateway_response or {}), "failure": gateway_response}
        await self._session.flush()

        payment = await self._payment_of(refund, for_update=True)
        if payment is not None:
            payment.amount_refunded = await self.completed_total(payment)
            if was_completed:
                payment.payment_data = {
                    **(payment.payment_data or {}),
                    "needs_attention": f"Completed refund {refund.refund_number} was reversed by the provider.",
                }
            self._audit(
                payment,
                reason=f"Refund {refund.refund_number} failed: {reason}",
                refund=refund,
                actor_id=actor_id,
                source=source,
                provider_reference=provider_reference,
            )
        if was_completed:
            booking = await self._booking_of(refund)
            if booking is not None and payment is not None:
                booking.payment_status = (
                    "partially_refunded" if _dec(payment.amount_refunded) > ZERO else "success"
                )
        await self._session.flush()
        logger.warning("refund_failed", refund_number=refund.refund_number, reason=reason, source=source)
        return refund

    # ── Event ─────────────────────────────────────────────────────────────

    async def publish_completed_event(
        self, refund: Refund, booking: Any, payment: Payment | None, fully: bool
    ) -> None:
        from app.events.contracts import BookingRefundCompletedEvent

        c = (getattr(booking, "customer_data", None) or refund.customer_data or {}) if booking else (refund.customer_data or {})
        b = (getattr(booking, "branch_data", None) or {}) if booking else {}
        s = (getattr(booking, "service_data", None) or {}) if booking else {}
        name = str(
            c.get("name") or c.get("full_name") or f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or ""
        )
        lang = "en" if str(c.get("language_preference") or "").strip().lower().startswith("en") else "ar"
        booking_number = (getattr(booking, "booking_number", None) if booking else None) or (
            (refund.booking_data or {}).get("booking_number") or ""
        )
        event = BookingRefundCompletedEvent(
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            booking_id=str(refund.booking_id or ""),
            booking_number=booking_number,
            booking_reference=str(booking_number or refund.booking_id or ""),
            payment_id=str(payment.id) if payment else "",
            customer_id=str(refund.customer_id or ""),
            customer_name=name,
            customer_phone=str(c.get("phone_number") or c.get("phone") or ""),
            customer_email=str(c.get("email") or ""),
            customer_language=lang,
            language_preference=lang,
            branch_id=str(refund.branch_id or ""),
            branch_name=str(b.get("name") or ""),
            service_id=str(getattr(booking, "service_id", "") or "") if booking else "",
            service_name=str(s.get("name") or ""),
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
            processed_at=_iso(refund.processed_at),
            created_at=_iso(refund.created_at),
            completed_at=_iso(refund.completed_at),
            invoice_number=refund.invoice_number or "",
            credit_note_number=refund.credit_note_number or "",
            payment_total_amount=str(payment.total_amount) if payment else "0.000",
            payment_amount_refunded=str(payment.amount_refunded) if payment else "0.000",
            booking_payment_status=str(getattr(booking, "payment_status", "") or "") if booking else "",
            is_fully_refunded=fully,
        )
        try:
            import asyncio

            from app.events.sqs_client import get_sqs_client

            asyncio.create_task(get_sqs_client().publish_event(event))
            logger.info("refund_completed_event_published", refund_number=refund.refund_number)
        except Exception as exc:
            logger.warning("refund_completed_event_publish_failed", refund_number=refund.refund_number, error=str(exc))

    # ── Internals ─────────────────────────────────────────────────────────

    async def _payment_of(self, refund: Refund, *, for_update: bool = False) -> Payment | None:
        if not refund.payment_id:
            return None
        stmt = select(Payment).where(Payment.id == refund.payment_id)
        if for_update:
            stmt = stmt.with_for_update()
        res = await self._session.execute(stmt)
        if hasattr(res, "scalar_one_or_none"):
            try:
                val = res.scalar_one_or_none()
                if val is not None and isinstance(val, Payment):
                    return val
            except Exception:
                pass
        val = res.scalars().first()
        return val if isinstance(val, Payment) else None

    async def _booking_of(self, refund: Refund) -> Any:
        if not refund.booking_id:
            return None
        from app.booking.infrastructure.models import Booking

        stmt = select(Booking).where(Booking.id == refund.booking_id).with_for_update()
        res = await self._session.execute(stmt)
        if hasattr(res, "scalar_one_or_none"):
            try:
                val = res.scalar_one_or_none()
                if val is not None and isinstance(val, Booking):
                    return val
            except Exception:
                pass
        val = res.scalars().first()
        return val if isinstance(val, Booking) else None

    def _audit(
        self,
        payment: Payment,
        *,
        reason: str,
        refund: Refund,
        source: str,
        actor_id: str | None = None,
        actor_data: dict | None = None,
        provider_reference: str | None = None,
        old_status: str | None = None,
    ) -> None:
        """Immutable audit row."""
        self._session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status or payment.status,
                new_status=payment.status or "success",
                source=source,
                reason=reason,
                provider_reference=provider_reference,
                correlation_id=refund.refund_number,
                metadata_={
                    "refund_id": str(refund.id) if refund.id else None,
                    "refund_number": refund.refund_number,
                    "refund_status": refund.status,
                    "refunded_amount": str(refund.refunded_amount),
                    "amount_refunded_total": str(payment.amount_refunded),
                },
                change_by_user=str(actor_id) if actor_id else source,
                change_by_user_data=actor_data,
            )
        )

    @staticmethod
    def _booking_snapshot(booking: Any) -> dict[str, Any]:
        start = getattr(booking, "appointment_start", None)
        return {
            "booking_number": getattr(booking, "booking_number", None),
            "status": getattr(booking, "status", None),
            "payment_status": getattr(booking, "payment_status", None),
            "appointment_start": _iso(start) or None,
            "service_id": str(booking.service_id) if getattr(booking, "service_id", None) else None,
            "branch_id": str(booking.branch_id) if getattr(booking, "branch_id", None) else None,
            "total_amount": str(getattr(booking, "total_amount", "") or ""),
        }
