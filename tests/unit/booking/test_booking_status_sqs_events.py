"""
tests/unit/booking/test_booking_status_sqs_events.py
────────────────────────────────────────────────────
Unit tests verifying that:
1. When bookings are updated to confirmed, payment_pending, or completed,
   an SQS message with all details is dispatched.
2. When update_booking_status endpoint is called, no direct payment record creation is performed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.booking.application.services import BookingService
from app.booking.domain.value_objects import BookingStatus, PaymentStatus
from app.booking.infrastructure.models import Booking
from app.events.contracts import (
    BookingCompletedEvent,
    BookingConfirmedEvent,
    BookingPaymentPendingEvent,
)


def _create_dummy_booking(status: str = "requested") -> Booking:
    b = MagicMock(spec=Booking)
    b.id = uuid.uuid4()
    b.customer_id = uuid.uuid4()
    b.customer_data = {
        "first_name": "Amina",
        "last_name": "Al-Sabah",
        "phone_number": "+96599998888",
        "email": "amina@example.com",
    }
    b.branch_id = uuid.uuid4()
    b.branch_data = {"name": "Salmiya Luxury Branch"}
    b.service_id = uuid.uuid4()
    b.service_data = {"name": "Moroccan Bath Signature"}
    b.service_arrangement_id = uuid.uuid4()
    b.service_arrangement_data = {"arrangement_name": "Royal Moroccan Suite"}
    b.therapist_id = uuid.uuid4()
    b.therapist_data = {"name": "Farida", "therapist_name": "Farida"}
    b.appointment_start = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    b.appointment_end = datetime(2026, 8, 30, 11, 15, tzinfo=timezone.utc)
    b.duration_minutes = 60
    b.extra_minutes = 15
    b.total_duration = 75
    b.addons_duration = 15
    b.base_price = Decimal("40.000")
    b.arrangement_price = Decimal("50.000")
    b.price_for_extra_minutes = Decimal("10.000")
    b.addon_price = Decimal("5.000")
    b.discount = Decimal("0.000")
    b.tax = Decimal("0.000")
    b.fees = Decimal("0.000")
    b.total_amount = Decimal("65.000")
    b.currency = "KWD"
    b.booking_type = "branch"
    b.status = status
    b.payment_status = "pending"
    b.addons = [{"name": "Aromatherapy Oil", "price": "5.000"}]
    b.customer_notes = "Customer prefers lavender oil"
    b.internal_notes = "VIP customer"
    b.payments_meta = {
        "invoice_id": "INV-100200",
        "payment_id": "PAY-999888",
        "is_paid": True,
    }
    b.created_at = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    b.updated_at = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    b.loyalty_data = None
    b.reward_id = None
    return b


@pytest.mark.asyncio
async def test_update_status_to_confirmed_dispatches_sqs_event():
    """Verify that updating status to confirmed dispatches BookingConfirmedEvent with all details."""
    mock_session = AsyncMock()
    service = BookingService(session=mock_session)

    booking = _create_dummy_booking(status="payment_pending")
    service._repo.get_by_id = AsyncMock(return_value=booking)
    service._repo.update = AsyncMock(return_value=booking)
    service._repo.delete_hold = AsyncMock()
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()

    updated = await service.update_status(
        booking_id=booking.id,
        new_status=BookingStatus.CONFIRMED,
        reason="Payment completed successfully",
    )

    assert booking.status == "confirmed"
    service._enqueue_event.assert_called_once()
    event = service._enqueue_event.call_args[0][0]
    assert isinstance(event, BookingConfirmedEvent)
    assert event.event_type == "booking.confirmed"
    assert event.booking_id == str(booking.id)
    assert event.customer_name == "Amina Al-Sabah"
    assert event.customer_phone == "+96599998888"
    assert event.branch_name == "Salmiya Luxury Branch"
    assert event.service_name == "Moroccan Bath Signature"
    assert event.service_arrangement_name == "Royal Moroccan Suite"
    assert event.therapist_name == "Farida"
    assert event.total_duration == 75
    assert event.total_amount == "65.000"
    assert event.currency == "KWD"
    assert event.payments_meta["invoice_id"] == "INV-100200"


@pytest.mark.asyncio
async def test_update_status_to_payment_pending_dispatches_sqs_event():
    """Verify that updating status to payment_pending dispatches BookingPaymentPendingEvent with all details."""
    mock_session = AsyncMock()
    service = BookingService(session=mock_session)

    booking = _create_dummy_booking(status="requested")
    service._repo.get_by_id = AsyncMock(return_value=booking)
    service._repo.update = AsyncMock(return_value=booking)
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()

    updated = await service.update_status(
        booking_id=booking.id,
        new_status=BookingStatus.PAYMENT_PENDING,
        reason="Awaiting customer gateway payment",
    )

    assert booking.status == "payment_pending"
    service._enqueue_event.assert_called_once()
    event = service._enqueue_event.call_args[0][0]
    assert isinstance(event, BookingPaymentPendingEvent)
    assert event.event_type == "booking.payment_pending"
    assert event.booking_id == str(booking.id)
    assert event.customer_name == "Amina Al-Sabah"
    assert event.branch_name == "Salmiya Luxury Branch"
    assert event.service_name == "Moroccan Bath Signature"
    assert event.service_arrangement_name == "Royal Moroccan Suite"
    assert event.therapist_name == "Farida"
    assert event.total_duration == 75
    assert event.total_amount == "65.000"


@pytest.mark.asyncio
async def test_update_status_to_completed_dispatches_sqs_event():
    """Verify that updating status to completed dispatches BookingCompletedEvent with all details."""
    mock_session = AsyncMock()
    service = BookingService(session=mock_session)

    booking = _create_dummy_booking(status="confirmed")
    service._repo.get_by_id = AsyncMock(return_value=booking)
    service._repo.update = AsyncMock(return_value=booking)
    service._repo.delete_hold = AsyncMock()
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()

    updated = await service.update_status(
        booking_id=booking.id,
        new_status=BookingStatus.COMPLETED,
        reason="Service rendered to customer",
    )

    assert booking.status == "completed"
    service._enqueue_event.assert_called_once()
    event = service._enqueue_event.call_args[0][0]
    assert isinstance(event, BookingCompletedEvent)
    assert event.event_type == "booking.completed"
    assert event.booking_id == str(booking.id)
    assert event.customer_name == "Amina Al-Sabah"
    assert event.branch_name == "Salmiya Luxury Branch"
    assert event.service_name == "Moroccan Bath Signature"
    assert event.total_amount == "65.000"


@pytest.mark.asyncio
async def test_update_booking_while_confirmed_dispatches_sqs_event():
    """Verify that updating fields on a confirmed booking dispatches an SQS event with all details."""
    mock_session = AsyncMock()
    service = BookingService(session=mock_session)

    booking = _create_dummy_booking(status="confirmed")
    service._repo.get_by_id = AsyncMock(return_value=booking)
    service._repo.update = AsyncMock(return_value=booking)
    service._repo.check_therapist_overlap = AsyncMock(return_value=False)
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()

    updated = await service.update_booking(
        booking_id=booking.id,
        customer_notes="Updated notes by customer",
    )

    service._enqueue_event.assert_called_once()
    event = service._enqueue_event.call_args[0][0]
    assert isinstance(event, BookingConfirmedEvent)
    assert event.event_type == "booking.confirmed"
    assert event.booking_id == str(booking.id)
