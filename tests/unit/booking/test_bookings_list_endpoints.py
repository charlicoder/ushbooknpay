"""
tests/unit/booking/test_bookings_list_endpoints.py
─────────────────────────────────────────────────
Unit tests for public /api/v1/bookings/ and private /api/v1/my-bookings/ endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from starlette.datastructures import URL

from app.api.deps import CurrentUser
from app.api.v1.bookings import list_bookings, list_my_bookings, _booking_to_list_item
from app.booking.application.services import BookingService
from app.booking.domain.value_objects import BookingStatus, PaymentStatus
from app.booking.infrastructure.models import Booking


def _create_mock_booking(
    booking_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    status: str = "confirmed",
    payment_status: str = "success",
) -> Booking:
    b = MagicMock(spec=Booking)
    b.id = booking_id or uuid.uuid4()
    b.customer_id = customer_id or uuid.uuid4()
    b.customer_data = {"name": "Test User", "email": "test@example.com", "phone": "+96512345678"}
    b.branch_id = uuid.uuid4()
    b.branch_data = {"branch_name": "Al Khiran Retreat"}
    b.service_id = uuid.uuid4()
    b.service_data = {"service_name": "Turkish Bath"}
    b.service_arrangement_id = uuid.uuid4()
    b.service_arrangement_data = {"arrangement_name": "VIP Suite"}
    b.therapist_id = uuid.uuid4()
    b.therapist_data = {"therapist_name": "Dana"}
    b.appointment_start = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    b.appointment_end = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
    b.duration_minutes = 60
    b.extra_minutes = 0
    b.total_duration = 75
    b.addons_duration = 15
    b.base_price = "40.000"
    b.status = status
    b.payment_status = payment_status
    b.payments_meta = {"invoice_id": "7106599", "payment_gateway": "KNET"}
    b.total_amount = "45.000"
    b.currency = "KWD"
    b.created_at = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    b.loyalty_data = None
    b.reward_id = None
    return b


def test_booking_to_list_item_serialization():
    b = _create_mock_booking()
    item = _booking_to_list_item(b)
    assert item.id == str(b.id)
    assert item.customer_id == str(b.customer_id)
    assert item.status == "confirmed"
    assert item.payment_status == "success"
    assert item.payments_meta["payment_gateway"] == "KNET"
    assert item.total_amount == "45.000"
    assert item.total_duration == 75
    assert item.addons_duration == 15
    assert item.base_price == "40.000"


@pytest.mark.asyncio
async def test_public_list_bookings_endpoint():
    """Verify that public list_bookings does not require user token and forwards filters."""
    mock_service = MagicMock(spec=BookingService)
    booking1 = _create_mock_booking(status="confirmed")
    mock_service.list_bookings = AsyncMock(return_value=([booking1], 1))

    mock_request = MagicMock(spec=Request)
    mock_request.url = URL("http://testserver/api/v1/bookings/?status=confirmed&date=2026-08-26")

    response = await list_bookings(
        booking_service=mock_service,
        request=mock_request,
        page=1,
        page_size=20,
        status="confirmed",
        payment_status="success",
        date="2026-08-26",
    )

    assert response.status_code == 200
    mock_service.list_bookings.assert_called_once_with(
        page=1,
        page_size=20,
        status="confirmed",
        payment_status="success",
        date_str="2026-08-26",
        from_date=None,
        to_date=None,
        customer_id=None,
        branch_id=None,
        therapist_id=None,
        service_id=None,
        service_arrangement_id=None,
        search=None,
    )


@pytest.mark.asyncio
async def test_private_list_my_bookings_endpoint():
    """Verify that list_my_bookings enforces customer_id from current_user."""
    mock_service = MagicMock(spec=BookingService)
    cust_id = uuid.uuid4()
    booking1 = _create_mock_booking(customer_id=cust_id)
    mock_service.list_customer_bookings = AsyncMock(return_value=([booking1], 1))

    current_user = CurrentUser(
        sub=str(cust_id),
        roles=["customer"],
        token="test-token",
    )

    mock_request = MagicMock(spec=Request)
    mock_request.url = URL("http://testserver/api/v1/my-bookings/?status=confirmed")

    response = await list_my_bookings(
        current_user=current_user,
        booking_service=mock_service,
        request=mock_request,
        page=1,
        page_size=20,
        status="confirmed",
        payment_status=None,
        date=None,
        from_date=None,
        to_date=None,
    )

    assert response.status_code == 200
    mock_service.list_customer_bookings.assert_called_once_with(
        customer_id=cust_id,
        page=1,
        page_size=20,
        status_filter="confirmed",
        payment_status_filter=None,
        date_str=None,
        from_date=None,
        to_date=None,
    )
