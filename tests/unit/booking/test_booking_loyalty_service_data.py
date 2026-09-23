"""
tests/unit/booking/test_booking_loyalty_service_data.py
───────────────────────────────────────────────────────
Unit tests to ensure loyalty_points, price_in_points, and is_eligible_for_loyalty
in service_data are correctly populated from database service details in both:
  1. The booking database record (service_data).
  2. The booking confirmed event payload (service_data + top-level loyalty_points).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.booking.application.services import BookingService, _build_booking_event_data
from app.booking.infrastructure.models import Booking
from app.booking.interfaces.schemas import CreateBookingRequest
from app.core.security import TokenPayload


def test_build_booking_event_data_includes_loyalty_points_in_service_data():
    """Verify that _build_booking_event_data explicitly embeds loyalty_points in service_data."""
    booking = Booking(
        id=uuid.uuid4(),
        booking_number="BK-9999",
        customer_id=uuid.uuid4(),
        customer_data={"first_name": "Fatima", "phone_number": "+96512345678"},
        branch_id=uuid.uuid4(),
        branch_data={"name": "Downtown Branch"},
        service_id=uuid.uuid4(),
        service_data={
            "id": "de9d93d5-cbf8-41c9-98a4-a2a261bbb1b1",
            "name": "Moroccan Bath",
            "loyalty_points": 25,
            "price_in_points": 250,
            "is_eligible_for_loyalty": True,
        },
        service_arrangement_id=uuid.uuid4(),
        service_arrangement_data={
            "name": "VIP Suite",
            "loyalty_points": 35,
            "price_in_points": 350,
        },
        appointment_date=datetime(2026, 9, 25).date(),
        appointment_start=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        base_price=Decimal("45.000"),
        total_amount=Decimal("45.000"),
        status="confirmed",
        payment_status="paid",
    )

    ev_data = _build_booking_event_data(booking)

    # 1. Event top-level fields
    assert ev_data["loyalty_points"] == 25
    assert ev_data["arrangement_loyalty_points"] == 35
    assert ev_data["price_in_points"] == 250
    assert ev_data["arrangement_price_in_points"] == 350
    assert ev_data["is_eligible_for_loyalty"] is True

    # 2. Event service_data dictionary
    service_data = ev_data["service_data"]
    assert service_data["loyalty_points"] == 25
    assert service_data["price_in_points"] == 250
    assert service_data["is_eligible_for_loyalty"] is True

    # 3. Event service_arrangement_data dictionary
    arr_data = ev_data["service_arrangement_data"]
    assert arr_data["loyalty_points"] == 35
    assert arr_data["price_in_points"] == 350


@pytest.mark.asyncio
async def test_ensure_booking_loyalty_details_refreshes_from_ushauth():
    """Verify that _ensure_booking_loyalty_details updates booking.service_data from ushauth if loyalty_points is 0."""
    service_id = uuid.uuid4()
    arrangement_id = uuid.uuid4()

    booking = Booking(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        service_id=service_id,
        service_data={
            "id": str(service_id),
            "name": "Swedish Massage",
            "loyalty_points": 0,  # Missing or zero
            "price_in_points": 0,
            "is_eligible_for_loyalty": False,
        },
        service_arrangement_id=arrangement_id,
        service_arrangement_data={
            "id": str(arrangement_id),
            "name": "Standard Room",
            "loyalty_points": None,
        },
    )

    mock_ushauth = MagicMock()
    mock_ushauth.get_service = AsyncMock(
        return_value={
            "id": str(service_id),
            "name": "Swedish Massage",
            "loyalty_points": 30,
            "price_in_points": 300,
            "is_eligible_for_loyalty": True,
        }
    )
    mock_ushauth.get_service_arrangement = AsyncMock(
        return_value={
            "id": str(arrangement_id),
            "arrangement_services": [
                {
                    "service_id": str(service_id),
                    "loyalty_points": 40,
                    "price_in_points": 400,
                }
            ],
        }
    )

    mock_session = AsyncMock()
    service = BookingService(session=mock_session, ushauth_client=mock_ushauth)
    service._repo.update = AsyncMock()

    await service._ensure_booking_loyalty_details(booking)

    # Verify booking.service_data was updated with values from database service
    assert booking.service_data["loyalty_points"] == 30
    assert booking.service_data["price_in_points"] == 300
    assert booking.service_data["is_eligible_for_loyalty"] is True

    # Verify service_arrangement_data was updated with arrangement override
    assert booking.service_arrangement_data["loyalty_points"] == 40
    assert booking.service_arrangement_data["price_in_points"] == 400

    service._repo.update.assert_awaited_once_with(booking)


def test_build_booking_event_data_zeros_loyalty_points_for_redemption_bookings():
    """Verify that _build_booking_event_data zeroes out loyalty earn points for redemption bookings."""
    booking = Booking(
        id=uuid.uuid4(),
        booking_number="BK-LOYALTY-1",
        customer_id=uuid.uuid4(),
        customer_data={"first_name": "Amina", "phone_number": "+96599998888"},
        branch_id=uuid.uuid4(),
        branch_data={"name": "Salmiya Branch"},
        service_id=uuid.uuid4(),
        service_data={
            "id": "de9d93d5-cbf8-41c9-98a4-a2a261bbb1b1",
            "name": "Moroccan Bath",
            "loyalty_points": 50,  # Catalogue awards 50 points on paid visits
            "price_in_points": 250,
            "is_eligible_for_loyalty": True,
        },
        service_arrangement_id=uuid.uuid4(),
        service_arrangement_data={
            "name": "VIP Suite",
            "loyalty_points": 70,
            "price_in_points": 350,
        },
        appointment_date=datetime(2026, 9, 25).date(),
        appointment_start=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        base_price=Decimal("0.000"),
        total_amount=Decimal("0.000"),
        booking_type="loyalty",
        payment_type="rewarded",
        status="confirmed",
        payment_status="rewarded",
    )

    ev_data = _build_booking_event_data(booking)

    # Top-level event fields must NOT allow points to be earned
    assert ev_data["is_eligible_for_loyalty"] is False
    assert ev_data["loyalty_points"] == 0
    assert ev_data["arrangement_loyalty_points"] is None

    # Service data snapshot in event must also reflect 0 earn points
    assert ev_data["service_data"]["is_eligible_for_loyalty"] is False
    assert ev_data["service_data"]["loyalty_points"] == 0


@pytest.mark.asyncio
async def test_ensure_booking_loyalty_details_skips_redemption_bookings():
    """Verify that _ensure_booking_loyalty_details does not refresh earn points for loyalty bookings."""
    service_id = uuid.uuid4()
    booking = Booking(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        service_id=service_id,
        service_data={
            "id": str(service_id),
            "name": "Full Body Massage",
            "loyalty_points": 0,
            "price_in_points": 200,
            "is_eligible_for_loyalty": False,
        },
        booking_type="loyalty",
        payment_type="rewarded",
        status="confirmed",
        payment_status="rewarded",
    )

    mock_ushauth = MagicMock()
    mock_ushauth.get_service = AsyncMock()

    mock_session = AsyncMock()
    service = BookingService(session=mock_session, ushauth_client=mock_ushauth)
    service._repo.update = AsyncMock()

    await service._ensure_booking_loyalty_details(booking)

    mock_ushauth.get_service.assert_not_awaited()
    service._repo.update.assert_not_awaited()
    assert booking.service_data["loyalty_points"] == 0
    assert booking.service_data["is_eligible_for_loyalty"] is False


@pytest.mark.asyncio
async def test_credit_points_skips_loyalty_redemption_booking():
    """Verify LoyaltyService.credit_points skips crediting points when booking was a redemption booking."""
    from app.loyalty.application.services import LoyaltyService
    from app.loyalty.infrastructure.models import LoyaltyAccount

    customer_id = uuid.uuid4()
    booking_id = uuid.uuid4()

    mock_booking = MagicMock()
    mock_booking.booking_type = "loyalty"
    mock_booking.payment_type = "rewarded"
    mock_booking.payment_status = "rewarded"
    mock_booking.reward_id = uuid.uuid4()
    mock_booking.loyalty_data = {"points_cost": 200}

    mock_account = LoyaltyAccount(
        id=uuid.uuid4(),
        customer_id=customer_id,
        balance_points=150,
        total_earned=500,
        total_redeemed=350,
    )

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_booking))
    )

    loyalty_svc = LoyaltyService(session=mock_session)
    loyalty_svc._repo.get_account_by_customer = AsyncMock(return_value=mock_account)
    loyalty_svc._repo.credit_points = AsyncMock()

    account = await loyalty_svc.credit_points(
        customer_id=customer_id,
        loyalty_points=50,
        arrangement_loyalty_points=None,
        booking_id=booking_id,
        booking_number="BK-TEST",
    )

    loyalty_svc._repo.credit_points.assert_not_awaited()
    assert account.balance_points == 150

