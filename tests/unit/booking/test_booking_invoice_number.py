"""
tests/unit/booking/test_booking_invoice_number.py
─────────────────────────────────────────────────
Unit tests for invoice_number on bookings:
- Schema parsing (invoice_number / invoiceNumber)
- ORM model attribute
- Event payload generation and dataclass contract acceptance
"""

from __future__ import annotations

import uuid
from decimal import Decimal
import pytest

from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
from app.booking.infrastructure.models import Booking
from app.booking.interfaces.schemas import (
    CreateBookingRequest,
    UpdateBookingRequest,
    UpdateBookingStatusRequest,
    BookingDetailResponse,
    BookingListItem,
)
from app.booking.application.services import _build_booking_event_data
from app.events.contracts import (
    BookingConfirmedEvent,
    BookingCreatedEvent,
    BookingRequestedEvent,
    BookingPaymentPendingEvent,
    BookingCancelledEvent,
    BookingCompletedEvent,
    BookingStatusUpdatedEvent,
)


def test_create_booking_request_invoice_number():
    req1 = CreateBookingRequest(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        appointment_date="2026-10-10",
        appointment_start_time="10:00",
        duration_minutes=60,
        invoice_number="INV/2026/10/00001",
    )
    assert req1.invoice_number == "INV/2026/10/00001"

    req2 = CreateBookingRequest.model_validate({
        "customer_id": str(uuid.uuid4()),
        "service_id": str(uuid.uuid4()),
        "branch_id": str(uuid.uuid4()),
        "service_arrangement_id": str(uuid.uuid4()),
        "appointment_date": "2026-10-10",
        "appointment_start_time": "10:00",
        "duration_minutes": 60,
        "invoiceNumber": "INV/2026/10/00002",
    })
    assert req2.invoice_number == "INV/2026/10/00002"


def test_update_booking_request_invoice_number():
    req1 = UpdateBookingRequest(invoice_number="INV/2026/10/00003")
    assert req1.invoice_number == "INV/2026/10/00003"

    req2 = UpdateBookingRequest.model_validate({"invoiceNumber": "INV/2026/10/00004"})
    assert req2.invoice_number == "INV/2026/10/00004"


def test_booking_model_invoice_number():
    booking = Booking(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        appointment_start=None,
        appointment_end=None,
        duration_minutes=60,
        arrangement_price=Decimal("50.000"),
        total_amount=Decimal("50.000"),
        status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.SUCCESS,
        invoice_number="INV/2026/10/00005",
    )
    assert booking.invoice_number == "INV/2026/10/00005"


def test_booking_event_data_and_contracts_invoice_number():
    booking = Booking(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        appointment_start=None,
        appointment_end=None,
        duration_minutes=60,
        arrangement_price=Decimal("50.000"),
        price_for_extra_minutes=Decimal("0.000"),
        addon_price=Decimal("0.000"),
        discount=Decimal("0.000"),
        tax=Decimal("0.000"),
        fees=Decimal("0.000"),
        total_amount=Decimal("50.000"),
        currency="KWD",
        status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.SUCCESS,
        invoice_number="INV/2026/10/00006",
    )
    ev_data = _build_booking_event_data(booking)
    assert ev_data["invoice_number"] == "INV/2026/10/00006"

    # Verify event contracts accept invoice_number
    confirmed = BookingConfirmedEvent(**ev_data)
    assert confirmed.invoice_number == "INV/2026/10/00006"

    pending = BookingPaymentPendingEvent(**ev_data)
    assert pending.invoice_number == "INV/2026/10/00006"

    completed = BookingCompletedEvent(**ev_data)
    assert completed.invoice_number == "INV/2026/10/00006"

    status_updated = BookingStatusUpdatedEvent(**ev_data)
    assert status_updated.invoice_number == "INV/2026/10/00006"

    ev_cancel = dict(ev_data)
    ev_cancel.pop("change_by_user", None)
    ev_cancel.pop("change_by_user_data", None)
    cancelled = BookingCancelledEvent(**ev_cancel)
    assert cancelled.invoice_number == "INV/2026/10/00006"
