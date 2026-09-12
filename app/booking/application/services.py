"""
app/booking/application/services.py
─────────────────────────────────────
Booking Application Service — orchestrates domain objects, repositories,
and external integrations to fulfil booking use cases.

Use cases implemented:
1. create_booking        — atomic create with double-booking check + outbox event
2. initiate_payment      — link payment record to booking
3. confirm_booking       — transition after payment success + outbox event
4. cancel_booking        — cancellation with optional refund trigger
5. request_reschedule    — validate window + fire event for admin approval
6. complete_booking      — mark appointment done
7. mark_no_show          — mark appointment no-show
8. get_booking           — detail retrieval
9. list_customer_bookings — paginated list

All methods are transactional — the session is managed by the caller (FastAPI dep).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.domain.rules import (
    assert_booking_is_reschedulable,
    assert_reschedule_window,
)
from app.booking.domain.state_machine import BookingStateMachine
from app.booking.domain.value_objects import (
    BookingStatus,
    PaymentStatus,
    PricingBreakdown,
)
from app.booking.infrastructure.models import (
    Booking,
    BookingStatusHistory,
    TemporaryHold,
)
from app.booking.infrastructure.repository import BookingRepository
from app.common.utils import utcnow
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    BookingNotFoundError,
    DoubleBookingError,
    TherapistNotAvailableError,
)
from app.core.logging import get_logger
from app.events.contracts import (
    BookingCancelledEvent,
    BookingConfirmedEvent,
    BookingCreatedEvent,
    BookingLoyaltyEvent,
    BookingNoShowEvent,
    BookingCompletedEvent,
    BookingPaymentPendingEvent,
    BookingPaymentStatusSuccessEvent,
    BookingStatusUpdatedEvent,
    RescheduleRequestedEvent,
)

logger = get_logger(__name__)


def _extract_customer_name(customer_data: dict | None) -> str:
    if not customer_data:
        return ""
    if name := customer_data.get("name"):
        return str(name)
    first = customer_data.get("first_name") or ""
    last = customer_data.get("last_name") or ""
    full = f"{first} {last}".strip()
    return full or str(customer_data.get("phone_number") or customer_data.get("email") or "")


def _extract_therapist_name(therapist_data: dict | None) -> str:
    if not therapist_data:
        return ""
    if name := therapist_data.get("name"):
        return str(name)
    first = therapist_data.get("first_name") or ""
    last = therapist_data.get("last_name") or ""
    return f"{first} {last}".strip()


def _build_booking_event_data(booking: Booking) -> dict[str, Any]:
    """Extract all booking fields into a comprehensive dictionary for SQS event contracts."""
    customer_dict = booking.customer_data or {}
    branch_dict = booking.branch_data or {}
    arr_dict = booking.service_arrangement_data or {}
    therapist_dict = booking.therapist_data or {}
    payment_data = booking.payment_data or {}

    # Always ensure is_eligible_for_loyalty is present inside service_data so
    # every SQS consumer (ushnotice, loyalty service, etc.) reads a consistent shape.
    _raw_service_dict = booking.service_data or {}
    is_eligible_for_loyalty: bool = bool(_raw_service_dict.get("is_eligible_for_loyalty"))
    service_dict = {**_raw_service_dict, "is_eligible_for_loyalty": is_eligible_for_loyalty}

    appt_start = booking.appointment_start
    appt_end = booking.appointment_end
    start_iso = appt_start.isoformat() if appt_start else ""
    end_iso = appt_end.isoformat() if appt_end else ""
    date_str = appt_start.strftime("%Y-%m-%d") if appt_start else ""
    start_time_str = appt_start.strftime("%H:%M") if appt_start else ""
    end_time_str = appt_end.strftime("%H:%M") if appt_end else ""

    booking_ref = (
        payment_data.get("invoice_reference")
        or payment_data.get("invoice_id")
        or payment_data.get("payment_id")
        or str(booking.id)
    )

    c_name = (
        _extract_customer_name(customer_dict)
        or str(payment_data.get("customer_name") or "")
    )
    c_phone = str(
        customer_dict.get("phone_number")
        or customer_dict.get("phone")
        or payment_data.get("customer_mobile")
        or ""
    )
    c_email = str(
        customer_dict.get("email")
        or payment_data.get("customer_email")
        or ""
    )

    branch_name = str(
        branch_dict.get("name")
        or branch_dict.get("branch_name")
        or ""
    )
    service_name = str(
        service_dict.get("name")
        or service_dict.get("service_name")
        or ""
    )
    arr_name = str(
        arr_dict.get("arrangement_name")
        or arr_dict.get("room_name")
        or arr_dict.get("name")
        or ""
    )
    therapist_name = (
        _extract_therapist_name(therapist_dict)
        or str(therapist_dict.get("therapist_name") or "")
    )

    pricing_dict = {
        "arrangement_price": str(booking.arrangement_price),
        "price_for_extra_minutes": str(booking.price_for_extra_minutes),
        "addon_price": str(booking.addon_price),
        "discount": str(booking.discount),
        "tax": str(booking.tax),
        "fees": str(booking.fees),
        "total": str(booking.total_amount),
        "currency": booking.currency,
    }

    b_type = getattr(booking, "booking_type", "branch_service")
    booking_type_str = b_type if isinstance(b_type, str) else "branch_service"
    p_type = getattr(booking, "payment_type", "service")
    payment_type_str = p_type if isinstance(p_type, str) else "service"

    return {
        "booking_id": str(booking.id),
        "booking_reference": str(booking_ref),
        "customer_id": str(booking.customer_id),
        "customer_name": c_name,
        "customer_phone": c_phone,
        "customer_email": c_email,
        "customer_data": customer_dict,
        "branch_id": str(booking.branch_id) if booking.branch_id else "",
        "branch_name": branch_name,
        "branch_data": branch_dict,
        "service_id": str(booking.service_id) if booking.service_id else "",
        "service_name": service_name,
        "service_data": service_dict,          # ← includes is_eligible_for_loyalty
        "service_arrangement_id": str(booking.service_arrangement_id) if booking.service_arrangement_id else "",
        "service_arrangement_name": arr_name,
        "service_arrangement_data": arr_dict,
        "therapist_id": str(booking.therapist_id) if booking.therapist_id else "",
        "therapist_name": therapist_name,
        "therapist_data": therapist_dict,
        "appointment_start": start_iso,
        "appointment_end": end_iso,
        "appointment_date": date_str,
        "appointment_starttime": start_time_str,
        "appointment_endtime": end_time_str,
        "appointment_time": start_time_str,
        "duration_minutes": booking.duration_minutes,
        "extra_minutes": booking.extra_minutes or 0,
        # Use the authoritative persisted total_duration column so the SQS event
        # matches the value returned by the PATCH /status/ response.  The column
        # already accounts for duration_minutes + extra_minutes + addons_duration
        # (set at booking-creation time).  Fall back to recomputing from parts
        # only for legacy rows where the column has never been populated.
        "total_duration": (
            getattr(booking, "total_duration", None)
            or (
                booking.duration_minutes
                + (booking.extra_minutes or 0)
                + (getattr(booking, "addons_duration", None) or 0)
            )
        ),
        "addons_duration": getattr(booking, "addons_duration", None) or 0,
        "booking_type": booking_type_str,
        "payment_type": payment_type_str,
        "status": booking.status,
        "payment_status": booking.payment_status,
        "total_amount": str(booking.total_amount),
        "currency": booking.currency,
        "pricing": pricing_dict,
        "addons": booking.addons or [],
        "customer_notes": booking.customer_notes,
        "internal_notes": booking.internal_notes,
        "payment_data": payment_data,
        "is_eligible_for_loyalty": is_eligible_for_loyalty,  # top-level for backward compat
        "created_at": booking.created_at.isoformat() if getattr(booking, "created_at", None) else "",
        "updated_at": booking.updated_at.isoformat() if getattr(booking, "updated_at", None) else "",
        "created_by": getattr(booking, "created_by", None) or "",
        # Loyalty booking fields (only populated for booking_type='loyalty')
        "loyalty_data": booking.loyalty_data or {},
        "reward_id": str(booking.reward_id) if getattr(booking, "reward_id", None) else "",
        # Gift voucher fields (populated when booking is paid with a gift voucher)
        "voucher_id": str(booking.voucher_id) if getattr(booking, "voucher_id", None) else "",
        "voucher_data": booking.voucher_data or {},
    }


def _extract_therapist_name(therapist_data: dict | None) -> str:
    if not therapist_data:
        return ""
    if name := therapist_data.get("name"):
        return str(name)
    first = therapist_data.get("first_name") or ""
    last = therapist_data.get("last_name") or ""
    return f"{first} {last}".strip()


class BookingService:
    """
    Booking application service.

    Dependencies are injected so they can be replaced in tests.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._repo = BookingRepository(session)
        self._settings = settings or get_settings()

    async def get_busy_therapist_ids(
        self,
        therapist_ids: list[uuid.UUID],
        start: datetime,
        end: datetime,
    ) -> set[uuid.UUID]:
        """Find all therapist IDs with active bookings or holds during [start, end)."""
        return await self._repo.get_busy_therapist_ids_in_range(therapist_ids, start, end)

    # ── Use Case 1: Create Booking ─────────────────────────────────────────

    async def create_booking(
        self,
        *,
        customer_id: uuid.UUID,
        customer_data: dict | None = None,
        branch_id: uuid.UUID | None = None,
        branch_data: dict | None = None,
        service_id: uuid.UUID,
        service_data: dict | None = None,
        service_arrangement_id: uuid.UUID | None = None,
        service_arrangement_data: dict | None = None,
        therapist_id: uuid.UUID,
        therapist_data: dict | None = None,
        appointment_start: datetime,
        appointment_end: datetime,
        duration_minutes: int,
        extra_minutes: int = 0,
        total_duration: int | None = None,
        addons_duration: int | None = None,
        base_price: Decimal | None = None,
        pricing: PricingBreakdown,
        addons: list[dict] | None = None,
        customer_notes: str | None = None,
        payment_data: dict | None = None,
        idempotency_key: str | None = None,
        booking_type: str = "branch_service",
        payment_type: str = "service",
        status: str = BookingStatus.REQUESTED.value,
        payment_status: str = PaymentStatus.NOT_INITIATED.value,
        loyalty_data: dict | None = None,
        reward_id: uuid.UUID | None = None,
        voucher_id: uuid.UUID | None = None,
        voucher_data: dict | None = None,
        created_by: str | None = None,
    ) -> Booking:
        """
        Create a new booking.

        Checks idempotency key first to handle duplicate requests.
        Performs double-booking detection with row-level locking.
        Creates a temporary hold for the appointment slot (unless already confirmed).
        Writes a BookingCreatedEvent (and BookingConfirmedEvent if confirmed) to SQS within the same flow.
        """
        # ── Idempotency check ────────────────────────────────────────────
        if idempotency_key:
            existing = await self._repo.get_by_idempotency_key(idempotency_key)
            if existing:
                logger.info("booking_idempotent_return", booking_id=str(existing.id))
                return existing

        # ── Double-booking check (with row lock) ─────────────────────────
        is_conflicting = await self._repo.check_therapist_overlap(
            therapist_id,
            appointment_start,
            appointment_end,
        )
        if is_conflicting:
            raise DoubleBookingError(
                f"Therapist {therapist_id} is already booked for the requested slot.",
                code="DOUBLE_BOOKING",
            )

        # ── Create Booking ────────────────────────────────────────────────
        booking = Booking(
            customer_id=customer_id,
            customer_data=customer_data or {},
            branch_id=branch_id,
            branch_data=branch_data or {},
            service_id=service_id,
            service_data=service_data or {},
            service_arrangement_id=service_arrangement_id,
            service_arrangement_data=service_arrangement_data or {},
            therapist_id=therapist_id,
            therapist_data=therapist_data or {},
            appointment_date=appointment_start.replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
            appointment_start=appointment_start,
            appointment_end=appointment_end,
            duration_minutes=duration_minutes,
            extra_minutes=extra_minutes,
            total_duration=total_duration,
            addons_duration=addons_duration,
            base_price=base_price,
            arrangement_price=pricing.arrangement_price,
            price_for_extra_minutes=pricing.price_for_extra_minutes,
            addon_price=pricing.addon_price,
            discount=pricing.discount,
            tax=pricing.tax,
            fees=pricing.fees,
            total_amount=pricing.total,
            currency=pricing.currency,
            status=status,
            payment_status=payment_status,
            booking_type=booking_type,
            payment_type=payment_type,
            addons=addons or [],
            customer_notes=customer_notes,
            payment_data=payment_data or {},
            idempotency_key=idempotency_key,
            loyalty_data=loyalty_data or {} if loyalty_data is not None else None,
            reward_id=reward_id,
            voucher_id=voucher_id,
            voucher_data=voucher_data,
            created_by=created_by,
        )

        booking = await self._repo.create(booking)

        # ── Temporary hold (only for unconfirmed branch bookings with a service arrangement) ─
        if service_arrangement_id is not None and booking.status != BookingStatus.CONFIRMED.value:
            hold = TemporaryHold(
                booking_id=booking.id,
                therapist_id=therapist_id,
                service_arrangement_id=service_arrangement_id,
                appointment_start=appointment_start,
                appointment_end=appointment_end,
                expires_at=utcnow() + timedelta(minutes=self._settings.TEMPORARY_HOLD_MINUTES),
            )
            await self._repo.create_hold(hold)

        # ── Status history ────────────────────────────────────────────────
        initial_status_enum = (
            BookingStatus(booking.status)
            if booking.status in BookingStatus._value2member_map_
            else BookingStatus.REQUESTED
        )
        await self._record_status_change(
            booking, None, initial_status_enum, source=created_by or "customer"
        )

        # ── Enqueue SQS event for Booking.Created ─────────────────────────
        customer_dict = booking.customer_data or {}
        branch_dict = booking.branch_data or {}
        service_dict = booking.service_data or {}
        therapist_dict = booking.therapist_data or {}

        await self._enqueue_event(
            BookingCreatedEvent(
                booking_id=str(booking.id),
                customer_id=str(booking.customer_id),
                customer_name=_extract_customer_name(customer_dict),
                customer_phone=str(customer_dict.get("phone_number") or customer_dict.get("phone") or ""),
                customer_email=str(customer_dict.get("email") or ""),
                branch_id=str(booking.branch_id),
                branch_name=str(branch_dict.get("name") or ""),
                service_id=str(booking.service_id),
                service_name=str(service_dict.get("name") or ""),
                therapist_id=str(booking.therapist_id),
                therapist_name=str(therapist_dict.get("name") or therapist_dict.get("full_name") or ""),
                appointment_start=booking.appointment_start.isoformat(),
                appointment_end=booking.appointment_end.isoformat(),
                duration_minutes=booking.duration_minutes,
                total_amount=str(booking.total_amount),
                currency=booking.currency,
                status=booking.status,
            )
        )

        logger.info("booking_created", booking_id=str(booking.id))

        # If a booking arrives already confirmed, fire BookingLoyaltyEvent or BookingConfirmedEvent immediately.
        if booking.status == BookingStatus.CONFIRMED.value:
            ev_data = _build_booking_event_data(booking)
            if (
                booking.booking_type == "loyalty"
                and booking.payment_status == PaymentStatus.REWARDED.value
            ):
                await self._enqueue_event(BookingLoyaltyEvent(**ev_data))
            else:
                await self._enqueue_event(BookingConfirmedEvent(**ev_data))
        elif booking.status == BookingStatus.PAYMENT_PENDING.value:
            ev_data = _build_booking_event_data(booking)
            await self._enqueue_event(BookingPaymentPendingEvent(**ev_data))

        return booking


    # ── Use Case 2: Update Booking (REST PATCH / PUT) ───────────────────────

    async def update_booking(
        self,
        booking_id: uuid.UUID,
        *,
        status: BookingStatus | None = None,
        payment_status: PaymentStatus | None = None,
        payment_data: dict | None = None,
        therapist_id: uuid.UUID | None = None,
        therapist_data: dict | None = None,
        appointment_start: datetime | None = None,
        extra_minutes: int | None = None,
        customer_notes: str | None = None,
        internal_notes: str | None = None,
        reason: str | None = None,
        source: str = "admin",
        changed_by: str | None = None,
        correlation_id: str | None = None,
        loyalty_data: dict | None = None,
        reward_id: uuid.UUID | None = None,
        voucher_id: uuid.UUID | None = None,
        voucher_data: dict | None = None,
    ) -> Booking:
        """
        Update booking fields (status, payment_status, payment_data, timing, therapist, notes).
        Follows REST standard for partial/full update.
        """
        booking = await self._repo.get_by_id(booking_id, for_update=True)

        # ── Update timing / therapist if provided ────────────────────────
        target_therapist_id = therapist_id or booking.therapist_id
        target_start = appointment_start or booking.appointment_start
        target_extra = extra_minutes if extra_minutes is not None else booking.extra_minutes
        total_duration = booking.duration_minutes + target_extra
        target_end = target_start + timedelta(minutes=total_duration)

        if therapist_id is not None or appointment_start is not None or extra_minutes is not None:
            # Check overlap against other bookings
            is_conflicting = await self._repo.check_therapist_overlap(
                target_therapist_id,
                target_start,
                target_end,
                exclude_booking_id=booking.id,
            )
            if is_conflicting:
                raise DoubleBookingError(
                    f"Therapist {target_therapist_id} is already booked for {target_start.isoformat()}.",
                    code="DOUBLE_BOOKING",
                )

            booking.therapist_id = target_therapist_id
            if therapist_data is not None:
                booking.therapist_data = therapist_data
            booking.extra_minutes = target_extra
            booking.appointment_start = target_start
            booking.appointment_end = target_end
            booking.appointment_date = target_start.replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        # ── Notes ────────────────────────────────────────────────────────
        if customer_notes is not None:
            booking.customer_notes = customer_notes
        if internal_notes is not None:
            booking.internal_notes = internal_notes

        # ── Payment status & Meta ────────────────────────────────────────
        if payment_status is not None:
            booking.payment_status = payment_status.value
        if payment_data is not None:
            booking.payment_data = {**(booking.payment_data or {}), **payment_data}

        # ── Loyalty fields ────────────────────────────────────────────
        if loyalty_data is not None:
            booking.loyalty_data = loyalty_data
        if reward_id is not None:
            booking.reward_id = reward_id

        # ── Gift Voucher fields ───────────────────────────────────────
        if voucher_id is not None:
            booking.voucher_id = voucher_id
        if voucher_data is not None:
            booking.voucher_data = voucher_data

        # ── Status update & event emission ───────────────────────────────
        if status is not None and BookingStatus(booking.status) != status:
            old_status = BookingStatus(booking.status)
            try:
                machine = BookingStateMachine(old_status)
                machine.transition_to(status)
            except Exception:
                logger.warning(
                    "booking_non_standard_transition",
                    booking_id=str(booking_id),
                    old_status=old_status.value,
                    new_status=status.value,
                    source=source,
                    reason=reason,
                )

            booking.status = status.value
            if reason:
                booking.internal_notes = (
                    f"{booking.internal_notes or ''}\n[{utcnow().isoformat()}] Status changed to {status.value} by {source}: {reason}"
                ).strip()

            if status in (BookingStatus.CONFIRMED, BookingStatus.CANCELLED, BookingStatus.COMPLETED):
                await self._repo.delete_hold(booking_id)

            await self._repo.update(booking)
            await self._record_status_change(
                booking,
                old_status,
                status,
                source=source,
                changed_by=changed_by,
                reason=reason,
                correlation_id=correlation_id,
            )

            # Dispatch SQS event
            await self._dispatch_status_event(booking, old_status, status, reason=reason, source=source)
        else:
            await self._repo.update(booking)
            try:
                current_status = BookingStatus(booking.status)
            except Exception:
                current_status = None
            if current_status in (BookingStatus.CONFIRMED, BookingStatus.PAYMENT_PENDING, BookingStatus.COMPLETED):
                await self._dispatch_status_event(
                    booking,
                    current_status,
                    current_status,
                    reason=reason or "Booking updated",
                    source=source,
                )

        logger.info("booking_updated", booking_id=str(booking.id))
        return booking

    # ── Use Case 3: Confirm Booking (after payment success) ────────────────

    async def confirm_booking(
        self,
        booking_id: uuid.UUID,
        *,
        payment_id: str,
        payment_data: dict | None = None,
        correlation_id: str | None = None,
    ) -> Booking:
        """
        Transition booking from PAYMENT_PENDING → CONFIRMED.

        Called by the payment webhook handler after verifying payment success.
        Removes the temporary hold (booking is now locked).
        Writes BookingConfirmedEvent to outbox/SQS.
        """
        booking = await self._repo.get_by_id(booking_id, for_update=True)

        current_status = BookingStatus(booking.status)

        # ── Idempotency guard ────────────────────────────────────────────────
        # If the booking is already confirmed (e.g. because PATCH /status/ was
        # called before POST /payments/ — the common "Paid on desk" flow) simply
        # merge the payment data and return without touching the status or firing
        # another BookingConfirmedEvent.  This avoids the BookingStateError that
        # the state machine raises for confirmed → confirmed transitions.
        if current_status == BookingStatus.CONFIRMED:
            if payment_data is not None:
                booking.payment_data = {**(booking.payment_data or {}), **payment_data}
                booking.payment_status = PaymentStatus.SUCCESS.value
                await self._repo.update(booking)
            logger.info(
                "booking_already_confirmed_payment_data_merged",
                booking_id=str(booking.id),
                payment_id=payment_id,
            )
            return booking

        old_status = current_status
        machine = BookingStateMachine(old_status)
        machine.transition_to(BookingStatus.CONFIRMED)

        booking.status = BookingStatus.CONFIRMED.value
        booking.payment_status = PaymentStatus.SUCCESS.value
        if payment_data is not None:
            booking.payment_data = {**(booking.payment_data or {}), **payment_data}

        await self._repo.update(booking)
        await self._repo.delete_hold(booking_id)
        await self._record_status_change(
            booking, old_status, BookingStatus.CONFIRMED,
            source="payment_webhook", changed_by=payment_id,
            correlation_id=correlation_id,
        )

        ev_data = _build_booking_event_data(booking)
        await self._enqueue_event(BookingConfirmedEvent(**ev_data))

        logger.info("booking_confirmed", booking_id=str(booking.id))
        return booking

    # ── Use Case 4: Cancel Booking ─────────────────────────────────────────

    async def cancel_booking(
        self,
        booking_id: uuid.UUID,
        *,
        reason: str = "",
        cancelled_by: str = "customer",
        refund_amount: Decimal | None = None,
        correlation_id: str | None = None,
    ) -> Booking:
        """Cancel a booking and emit a cancellation event."""
        booking = await self._repo.get_by_id(booking_id, for_update=True)
        machine = BookingStateMachine(BookingStatus(booking.status))
        old_status = BookingStatus(booking.status)

        machine.transition_to(BookingStatus.CANCELLED)
        booking.status = BookingStatus.CANCELLED.value
        booking.internal_notes = (
            f"{booking.internal_notes or ''}\nCancelled by {cancelled_by}: {reason}"
        ).strip()

        await self._repo.update(booking)
        await self._repo.delete_hold(booking_id)
        await self._record_status_change(
            booking, old_status, BookingStatus.CANCELLED,
            source=cancelled_by, reason=reason, correlation_id=correlation_id,
        )

        customer_dict = booking.customer_data or {}
        appt_start = booking.appointment_start
        appt_end = booking.appointment_end
        await self._enqueue_event(
            BookingCancelledEvent(
                booking_id=str(booking.id),
                customer_id=str(booking.customer_id),
                branch_id=str(booking.branch_id),
                service_id=str(booking.service_id),
                service_arrangement_id=str(booking.service_arrangement_id),
                therapist_id=str(booking.therapist_id),
                appointment_start=appt_start.isoformat(),
                appointment_end=appt_end.isoformat(),
                appointment_date=appt_start.strftime("%Y-%m-%d"),
                appointment_starttime=appt_start.strftime("%H:%M"),
                appointment_endtime=appt_end.strftime("%H:%M"),
                customer_name=_extract_customer_name(customer_dict),
                customer_phone=str(customer_dict.get("phone_number") or customer_dict.get("phone") or ""),
                cancellation_reason=reason,
                refund_issued=refund_amount is not None and refund_amount > 0,
                refund_amount=str(refund_amount) if refund_amount else None,
            )
        )

        logger.info("booking_cancelled", booking_id=str(booking.id), reason=reason)
        return booking

    # ── Use Case 5: Request Reschedule ─────────────────────────────────────

    async def request_reschedule(
        self,
        booking_id: uuid.UUID,
        *,
        new_start: datetime,
        new_end: datetime,
        customer_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> Booking:
        """
        Customer requests a reschedule.
        Validates reschedule window and ownership.
        """
        booking = await self._repo.get_by_id(booking_id, for_update=True)

        if booking.customer_id != customer_id:
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("You can only reschedule your own bookings.")

        assert_booking_is_reschedulable(BookingStatus(booking.status))
        assert_reschedule_window(booking.appointment_start)

        machine = BookingStateMachine(BookingStatus(booking.status))
        old_status = BookingStatus(booking.status)
        machine.transition_to(BookingStatus.RESCHEDULE_REQUESTED)

        booking.status = BookingStatus.RESCHEDULE_REQUESTED.value
        booking.internal_notes = (
            f"{booking.internal_notes or ''}\n"
            f"Reschedule requested: {new_start.isoformat()} to {new_end.isoformat()}"
        ).strip()

        await self._repo.update(booking)
        await self._record_status_change(
            booking, old_status, BookingStatus.RESCHEDULE_REQUESTED,
            source="customer", correlation_id=correlation_id,
        )

        await self._enqueue_event(
            RescheduleRequestedEvent(
                booking_id=str(booking.id),
                customer_id=str(booking.customer_id),
                current_appointment_start=booking.appointment_start.isoformat(),
                requested_appointment_start=new_start.isoformat(),
                requested_appointment_end=new_end.isoformat(),
            )
        )

        return booking

    # ── Use Case 6: Complete / No-Show ────────────────────────────────────

    async def complete_booking(self, booking_id: uuid.UUID) -> Booking:
        """Mark a booking as completed."""
        return await self.update_status(
            booking_id, BookingStatus.COMPLETED, source="system"
        )

    async def mark_no_show(self, booking_id: uuid.UUID) -> Booking:
        """Mark a booking as no-show."""
        return await self.update_status(
            booking_id, BookingStatus.NO_SHOW, source="system"
        )

    async def update_status(
        self,
        booking_id: uuid.UUID,
        new_status: BookingStatus,
        *,
        payment_status: PaymentStatus | None = None,
        payment_data: dict | None = None,
        reason: str | None = None,
        source: str = "admin",
        changed_by: str | None = None,
        correlation_id: str | None = None,
        loyalty_data: dict | None = None,
        reward_id: uuid.UUID | None = None,
        voucher_id: uuid.UUID | None = None,
        voucher_data: dict | None = None,
    ) -> Booking:
        """
        Update booking status (e.g. from admin panel or inter-service sync).
        Records audit trail in status history and dispatches SQS event.
        """
        return await self.update_booking(
            booking_id=booking_id,
            status=new_status,
            payment_status=payment_status,
            payment_data=payment_data,
            reason=reason,
            source=source,
            changed_by=changed_by,
            correlation_id=correlation_id,
            loyalty_data=loyalty_data,
            reward_id=reward_id,
            voucher_id=voucher_id,
            voucher_data=voucher_data,
        )

    # ── Use Case 7: Retrieval ──────────────────────────────────────────────

    async def get_booking(
        self, booking_id: uuid.UUID, *, load_history: bool = False
    ) -> Booking:
        return await self._repo.get_by_id(booking_id, load_history=load_history)

    async def list_bookings(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | BookingStatus | None = None,
        payment_status: str | None = None,
        date_str: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        customer_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        therapist_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        service_arrangement_id: uuid.UUID | None = None,
        search: str | None = None,
    ) -> tuple[list[Booking], int]:
        """List bookings across the platform with filtering and pagination."""
        return await self._repo.list_bookings(
            page=page,
            page_size=page_size,
            status=status,
            payment_status=payment_status,
            date_str=date_str,
            from_date=from_date,
            to_date=to_date,
            customer_id=customer_id,
            branch_id=branch_id,
            therapist_id=therapist_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
            search=search,
        )

    async def list_customer_bookings(
        self,
        customer_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: BookingStatus | str | None = None,
        payment_status_filter: str | None = None,
        date_str: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> tuple[list[Booking], int]:
        """List bookings for a specific customer."""
        return await self._repo.list_by_customer(
            customer_id,
            page=page,
            page_size=page_size,
            status_filter=status_filter,
            payment_status_filter=payment_status_filter,
            date_str=date_str,
            from_date=from_date,
            to_date=to_date,
        )

    # ── Private helpers ────────────────────────────────────────────────────

    async def _dispatch_status_event(
        self,
        booking: Booking,
        old_status: BookingStatus | str | None,
        new_status: BookingStatus | str,
        *,
        reason: str | None = None,
        source: str = "admin",
    ) -> None:
        ev_data = _build_booking_event_data(booking)
        new_status_val = new_status.value if hasattr(new_status, "value") else str(new_status)
        old_status_val = (
            old_status.value
            if (old_status and hasattr(old_status, "value"))
            else (str(old_status) if old_status else None)
        )

        if new_status in (BookingStatus.CONFIRMED, "confirmed"):
            await self._enqueue_event(BookingConfirmedEvent(**ev_data))
            # Also fire the loyalty-specific event when this is a loyalty reward redemption.
            if (
                booking.booking_type == "loyalty"
                and booking.payment_status == PaymentStatus.REWARDED.value
            ):
                await self._enqueue_event(BookingLoyaltyEvent(**ev_data))
        elif new_status in (BookingStatus.PAYMENT_PENDING, "payment_pending"):
            await self._enqueue_event(BookingPaymentPendingEvent(**ev_data))
        elif new_status in (BookingStatus.COMPLETED, "completed"):
            await self._enqueue_event(BookingCompletedEvent(**ev_data))
        elif new_status in (BookingStatus.CANCELLED, "cancelled"):
            await self._enqueue_event(
                BookingCancelledEvent(
                    booking_id=ev_data["booking_id"],
                    customer_id=ev_data["customer_id"],
                    branch_id=ev_data["branch_id"],
                    service_id=ev_data["service_id"],
                    service_arrangement_id=ev_data["service_arrangement_id"],
                    therapist_id=ev_data["therapist_id"],
                    appointment_start=ev_data["appointment_start"],
                    appointment_end=ev_data["appointment_end"],
                    appointment_date=ev_data["appointment_date"],
                    appointment_starttime=ev_data["appointment_starttime"],
                    appointment_endtime=ev_data["appointment_endtime"],
                    customer_name=ev_data["customer_name"],
                    customer_phone=ev_data["customer_phone"],
                    cancellation_reason=reason or "",
                    refund_issued=False,
                )
            )
        elif new_status in (BookingStatus.NO_SHOW, "no_show"):
            await self._enqueue_event(
                BookingNoShowEvent(
                    booking_id=ev_data["booking_id"],
                    customer_id=ev_data["customer_id"],
                    therapist_id=ev_data["therapist_id"] or None,
                )
            )
        else:
            await self._enqueue_event(
                BookingStatusUpdatedEvent(
                    **ev_data,
                    old_status=old_status_val,
                    new_status=new_status_val,
                    reason=reason,
                    source=source,
                )
            )

    async def _record_status_change(
        self,
        booking: Booking,
        old_status: BookingStatus | None,
        new_status: BookingStatus,
        *,
        source: str | None = None,
        changed_by: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        history = BookingStatusHistory(
            booking_id=booking.id,
            old_status=old_status.value if old_status else None,
            new_status=new_status.value,
            source=source,
            changed_by=changed_by,
            reason=reason,
            correlation_id=correlation_id,
        )
        await self._repo.save_status_history(history)

    async def _enqueue_event(self, event: object) -> None:
        """Publish event directly to AWS_SQS_NOTIFICATION_QUEUE_URL asynchronously."""
        import asyncio
        from app.events.sqs_client import get_sqs_client

        try:
            sqs = get_sqs_client()
            asyncio.create_task(sqs.publish_event(event))
        except Exception as exc:
            logger.error(
                "event_publish_failed",
                event_name=getattr(event, "event_name", type(event).__name__),
                error=str(exc),
            )
