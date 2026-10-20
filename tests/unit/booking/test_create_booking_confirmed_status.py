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


@pytest.mark.asyncio
async def test_api_create_loyalty_booking_creates_zero_invoice_and_payment(monkeypatch):
    """Verify that booking with loyalty points creates zero-amount invoice and payment record."""
    from unittest.mock import patch
    from app.payment.infrastructure.models import Payment

    mock_service = MagicMock(spec=BookingService)
    booking_id = uuid.uuid4()
    cust_id = uuid.uuid4()

    created_booking = MagicMock(spec=Booking)
    created_booking.id = booking_id
    created_booking.customer_id = cust_id
    created_booking.total_amount = Decimal("0.000")
    created_booking.status = "confirmed"
    created_booking.payment_status = "rewarded"
    created_booking.payment_type = "rewarded"
    created_booking.payment_data = {}
    created_booking.service_data = {"name": "Swedish Massage"}
    created_booking.branch_data = {"name": "Main Branch"}
    created_booking.service_arrangement_data = None
    created_booking.loyalty_data = {"points_cost": 50}
    created_booking.currency = "KWD"
    created_booking.invoice_number = None
    created_booking.payment_number = None
    created_booking.duration_minutes = 60
    created_booking.extra_minutes = 0
    created_booking.total_duration = 60
    created_booking.addons_duration = 0
    created_booking.addons = []
    created_booking.customer_notes = None
    created_booking.internal_notes = None
    created_booking.reward_id = None
    created_booking.voucher_id = None
    created_booking.voucher_data = None
    created_booking.created_at = None
    created_booking.created_by_user = "customer"
    created_booking.created_by_user_data = None

    mock_session = AsyncMock()
    mock_scalars_obj = MagicMock()
    mock_scalars_obj.first = MagicMock(return_value=None)
    mock_scalar = MagicMock()
    mock_scalar.scalars = MagicMock(return_value=mock_scalars_obj)
    mock_session.execute = AsyncMock(return_value=mock_scalar)
    mock_service._session = mock_session
    mock_service.create_booking = AsyncMock(return_value=created_booking)

    tid = uuid.uuid4()
    mock_ushauth = AsyncMock()
    mock_ushauth.check_appointment_availability = AsyncMock(return_value={"available": "yes", "therapist_id": str(tid)})
    mock_ushauth.get_customer_profile = AsyncMock(return_value={"data": {"user": {"first_name": "Noura", "last_name": "Test", "email": "noura@example.com", "phone_number": "+96511223344"}}})

    mock_request = MagicMock(spec=Request)
    mock_request.headers = Headers()

    body = CreateBookingRequest(
        customer_id=cust_id,
        service_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-10-25",
        appointment_time="14:00:00",
        base_price="0.000",
        base_duration=60,
        booking_type="loyalty",
        payment_type="rewarded",
        payment_status="rewarded",
        loyalty_data={"points_cost": 50},
    )

    mock_current_user = MagicMock()
    mock_current_user.sub = str(cust_id)
    mock_settings = MagicMock()

    # Mock UshanrClient and generate_payment_number
    mock_ushanr = AsyncMock()
    mock_ushanr.create_or_get_booking_invoice_data = AsyncMock(
        return_value={"invoice_id": "INV-ID-123", "invoice_number": "INV/2026/10/00099"}
    )
    mock_ushanr.aclose = AsyncMock()

    with patch("app.integrations.ushanr_client.UshanrClient", return_value=mock_ushanr), \
         patch("app.api.v1.payments.generate_payment_number", AsyncMock(return_value="PMT/2026/10/000001")), \
         patch("app.loyalty.application.services.LoyaltyService.redeem_points", AsyncMock()):

        response = await create_booking(
            request=mock_request,
            body=body,
            current_user=mock_current_user,
            booking_service=mock_service,
            ushauth=mock_ushauth,
            settings=mock_settings,
        )

    # 1. Invoice created and linked to booking
    assert created_booking.invoice_number == "INV/2026/10/00099"
    assert response.data.invoice_number == "INV/2026/10/00099"

    # 2. Payment added to session with zero amount and connected to invoice
    added_payments = [
        call.args[0] for call in mock_session.add.call_args_list
        if isinstance(call.args[0], Payment)
    ]
    assert len(added_payments) == 1
    payment = added_payments[0]
    assert payment.total_amount == Decimal("0")
    assert payment.invoice_number == "INV/2026/10/00099"
    assert payment.invoice_id == "INV-ID-123"
    assert payment.payment_number == "PMT/2026/10/000001"
    assert payment.payment_provider == "Loyalty Points"
    assert payment.payment_through == "ushspa"
    assert payment.status == "success"
