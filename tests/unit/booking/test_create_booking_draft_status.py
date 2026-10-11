"""
Unit tests for BookingStatus.DRAFT and draft booking creation behavior.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.booking.domain.state_machine import BookingStateMachine
from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
from app.booking.infrastructure.models import Booking


def test_booking_status_draft_value():
    assert BookingStatus.DRAFT == "draft"
    assert BookingStatus("draft") == BookingStatus.DRAFT


def test_draft_state_machine_transitions():
    sm = BookingStateMachine(BookingStatus.DRAFT)
    assert sm.can_transition_to(BookingStatus.CONFIRMED) is True
    assert sm.can_transition_to(BookingStatus.PAYMENT_PENDING) is True
    assert sm.can_transition_to(BookingStatus.REQUESTED) is True
    assert sm.can_transition_to(BookingStatus.CANCELLED) is True
    assert sm.can_transition_to(BookingStatus.PAYMENT_FAILED) is True
    assert sm.can_transition_to(BookingStatus.COMPLETED) is False


@pytest.mark.asyncio
async def test_create_booking_draft_skips_events():
    """Verify that when a booking is created with status=draft, no notification/SQS events are enqueued."""
    from app.booking.application.services import BookingService

    mock_repo = MagicMock()
    mock_repo.get_by_idempotency_key = AsyncMock(return_value=None)
    mock_repo.check_therapist_overlap = AsyncMock(return_value=False)
    mock_repo.generate_booking_number = AsyncMock(return_value="BOK/2026/10/000001")

    async def mock_create(booking):
        booking.id = uuid.uuid4()
        return booking

    mock_repo.create = AsyncMock(side_effect=mock_create)

    session = MagicMock()
    mock_settings = MagicMock()
    mock_settings.TEMPORARY_HOLD_MINUTES = 15
    service = BookingService(session=session, settings=mock_settings)
    service._repo = mock_repo
    service._enqueue_event = AsyncMock()
    service._record_status_change = AsyncMock()

    pricing = PricingBreakdown(
        arrangement_price=Decimal("20.000"),
        price_for_extra_minutes=Decimal("0.000"),
        addon_price=Decimal("0.000"),
        discount=Decimal("0.000"),
        tax=Decimal("0.000"),
        fees=Decimal("0.000"),
        total=Decimal("20.000"),
        currency="KWD",
    )

    now = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc)

    booking = await service.create_booking(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        therapist_id=uuid.uuid4(),
        appointment_start=now,
        appointment_end=end,
        duration_minutes=60,
        pricing=pricing,
        status=BookingStatus.DRAFT.value,
        payment_status=PaymentStatus.PENDING.value,
    )

    assert booking.status == BookingStatus.DRAFT.value
    # SQS events MUST NOT be enqueued for draft bookings
    service._enqueue_event.assert_not_called()
