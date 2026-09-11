"""
tests/unit/booking/test_create_booking_confirmed_status.py
─────────────────────────────────────────────────────────
Unit tests verifying that:
1. Creating a booking with status="confirmed" and payment_status="success"
   stores status and payment_status properly on the booking model.
2. SQS event BookingConfirmedEvent is enqueued when booking is created as confirmed.
3. Temporary hold is skipped for already-confirmed bookings.
4. Status history records initial status as confirmed.
5. The REST create_booking endpoint correctly parses status and payment_status
   from the request payload and reflects them in the response.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from app.api.v1.bookings import create_booking
from app.booking.application.services import BookingService
from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
from app.booking.infrastructure.models import Booking
from app.booking.interfaces.schemas import CreateBookingRequest
from app.events.contracts import BookingConfirmedEvent, BookingCreatedEvent


@pytest.mark.asyncio
async def test_service_create_booking_with_confirmed_and_success():
    """Verify that BookingService.create_booking respects status=confirmed and payment_status=success."""
    mock_session = AsyncMock()
    service = BookingService(session=mock_session)

    # Mocks
    service._repo.get_by_idempotency_key = AsyncMock(return_value=None)
    service._repo.check_therapist_overlap = AsyncMock(return_value=False)
    service._repo.create = AsyncMock(side_effect=lambda b: b)
    service._repo.create_hold = AsyncMock()
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()

    pricing = PricingBreakdown(
        arrangement_price=Decimal("40.000"),
        price_for_extra_minutes=Decimal("0.000"),
        addon_price=Decimal("5.000"),
        discount=Decimal("0.000"),
        tax=Decimal("0.000"),
        fees=Decimal("0.000"),
        total=Decimal("45.000"),
        currency="KWD",
    )

    booking = await service.create_booking(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        therapist_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        appointment_start=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        pricing=pricing,
        status="confirmed",
        payment_status="success",
        payment_data={"is_paid": True, "invoice_id": "INV-777"},
    )

    # Booking fields verified
    assert booking.status == "confirmed"
    assert booking.payment_status == "success"

    # Temporary hold must NOT be created when booking is already confirmed
    service._repo.create_hold.assert_not_called()

    # Status history must record initial status as CONFIRMED
    service._record_status_change.assert_called_once()
    assert service._record_status_change.call_args[0][2] == BookingStatus.CONFIRMED

    # Both BookingCreatedEvent and BookingConfirmedEvent must be enqueued
    assert service._enqueue_event.call_count == 2
    events = [call[0][0] for call in service._enqueue_event.call_args_list]
    assert any(isinstance(ev, BookingCreatedEvent) for ev in events)
    assert any(isinstance(ev, BookingConfirmedEvent) for ev in events)

    confirmed_ev = next(ev for ev in events if isinstance(ev, BookingConfirmedEvent))
    assert confirmed_ev.status == "confirmed"
    assert confirmed_ev.payment_status == "success"
    assert confirmed_ev.payment_data["invoice_id"] == "INV-777"


@pytest.mark.asyncio
async def test_api_create_booking_payload_confirmed_and_success():
    """Verify that POST /api/v1/bookings/ endpoint parses status=confirmed and payment_status=success."""
    mock_service = MagicMock(spec=BookingService)
    created_booking = MagicMock(spec=Booking)
    created_booking.id = uuid.uuid4()
    created_booking.customer_id = uuid.uuid4()
    created_booking.total_amount = Decimal("45.000")
    created_booking.status = "confirmed"
    created_booking.payment_status = "success"
    created_booking.payment_type = "service"
    created_booking.payment_data = {"is_paid": True}
    created_booking.service_data = {"is_eligible_for_loyalty": True}
    created_booking.loyalty_data = None
    created_booking.reward_id = None
    created_booking.voucher_id = None
    created_booking.voucher_data = None

    mock_service.create_booking = AsyncMock(return_value=created_booking)

    tid = uuid.uuid4()
    mock_ushauth = AsyncMock()
    mock_ushauth.check_appointment_availability = AsyncMock(return_value={"available": "yes", "therapist_id": str(tid)})
    mock_ushauth.get_customer_profile = AsyncMock(return_value={"data": {"user": {"first_name": "Dana", "last_name": "Test", "email": "dana@example.com", "phone_number": "+96512345678"}}})

    mock_request = MagicMock(spec=Request)
    mock_request.headers = Headers()

    body = CreateBookingRequest(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-09-20",
        appointment_time="10:00:00",
        base_price="40.000",
        base_duration=60,
        status="confirmed",
        payment_status="success",
        payment_data={"is_paid": True},
    )

    mock_current_user = MagicMock()
    mock_current_user.sub = str(uuid.uuid4())
    mock_settings = MagicMock()

    response = await create_booking(
        request=mock_request,
        body=body,
        current_user=mock_current_user,
        booking_service=mock_service,
        ushauth=mock_ushauth,
        settings=mock_settings,
    )

    # Verify create_booking called with status="confirmed" and payment_status="success"
    mock_service.create_booking.assert_called_once()
    kwargs = mock_service.create_booking.call_args[1]
    assert kwargs["status"] == "confirmed"
    assert kwargs["payment_status"] == "success"

    # Verify API response
    assert response.success is True
    assert response.data.status == "confirmed"
    assert response.data.payment_status == "success"


@pytest.mark.asyncio
async def test_api_create_booking_auto_infers_confirmed_from_paid_payment_data():
    """Verify that if payment_data indicates is_paid=True, status and payment_status default to confirmed/success."""
    mock_service = MagicMock(spec=BookingService)
    created_booking = MagicMock(spec=Booking)
    created_booking.id = uuid.uuid4()
    created_booking.customer_id = uuid.uuid4()
    created_booking.total_amount = Decimal("45.000")
    created_booking.status = "confirmed"
    created_booking.payment_status = "success"
    created_booking.payment_type = "service"
    created_booking.payment_data = {"is_paid": True}
    created_booking.service_data = {}
    created_booking.loyalty_data = None
    created_booking.reward_id = None
    created_booking.voucher_id = None
    created_booking.voucher_data = None

    mock_service.create_booking = AsyncMock(return_value=created_booking)

    tid = uuid.uuid4()
    mock_ushauth = AsyncMock()
    mock_ushauth.check_appointment_availability = AsyncMock(return_value={"available": "yes", "therapist_id": str(tid)})
    mock_ushauth.get_customer_profile = AsyncMock(return_value={"data": {"user": {"first_name": "Dana", "last_name": "Test", "email": "dana@example.com", "phone_number": "+96512345678"}}})

    mock_request = MagicMock(spec=Request)
    mock_request.headers = Headers()

    body = CreateBookingRequest(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-09-20",
        appointment_time="10:00:00",
        base_price="40.000",
        base_duration=60,
        # No explicit status or payment_status, but payment_data has is_paid=True
        payment_data={"is_paid": True, "status": "Paid"},
    )

    mock_current_user = MagicMock()
    mock_current_user.sub = str(uuid.uuid4())
    mock_settings = MagicMock()

    response = await create_booking(
        request=mock_request,
        body=body,
        current_user=mock_current_user,
        booking_service=mock_service,
        ushauth=mock_ushauth,
        settings=mock_settings,
    )


    mock_service.create_booking.assert_called_once()
    kwargs = mock_service.create_booking.call_args[1]
    assert kwargs["status"] == "confirmed"
    assert kwargs["payment_status"] == "success"
    assert response.data.status == "confirmed"
    assert response.data.payment_status == "success"
