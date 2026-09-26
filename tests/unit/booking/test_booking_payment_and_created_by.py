"""
tests/unit/booking/test_booking_payment_and_created_by.py
──────────────────────────────────────────────────────────
Unit tests for the new payment fields and created_by_user / created_by_user_data
on the bookings API, domain models, and schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.bookings import (
    _booking_to_detail,
    _booking_to_list_item,
    create_booking,
    update_booking,
)
from app.booking.domain.value_objects import (
    BookingPaymentGateway,
    BookingPaymentProvider,
    BookingPaymentThrough,
    BookingStatus,
    BookingType,
    PaymentStatus,
    PricingBreakdown,
)
from app.booking.infrastructure.models import Booking
from app.booking.interfaces.schemas import (
    BookingDetailResponse,
    BookingListItem,
    CreateBookingDataResponse,
    CreateBookingRequest,
    UpdateBookingRequest,
    UpdateBookingStatusRequest,
)


def test_booking_payment_enums_normalisation():
    """Test normalisation logic for BookingPaymentProvider, Gateway, and Through."""
    # Provider
    assert BookingPaymentProvider.normalise("MyFatoorah") == BookingPaymentProvider.MYFATOORAH
    assert BookingPaymentProvider.normalise("myfatoorah") == BookingPaymentProvider.MYFATOORAH
    assert BookingPaymentProvider.normalise("PaymentLink") == BookingPaymentProvider.PAYMENTLINK
    assert BookingPaymentProvider.normalise("DirectLink") == BookingPaymentProvider.PAYMENTLINK
    assert BookingPaymentProvider.normalise("direct") == BookingPaymentProvider.PAYMENTLINK
    assert BookingPaymentProvider.normalise("deema") == BookingPaymentProvider.DEEMA
    assert BookingPaymentProvider.normalise("unknown") == BookingPaymentProvider.OTHER
    assert BookingPaymentProvider.normalise(None) == BookingPaymentProvider.OTHER

    # Gateway
    assert BookingPaymentGateway.normalise("KNET") == BookingPaymentGateway.KNET
    assert BookingPaymentGateway.normalise("knet") == BookingPaymentGateway.KNET
    assert BookingPaymentGateway.normalise("TAP") == BookingPaymentGateway.TAP
    assert BookingPaymentGateway.normalise("tap") == BookingPaymentGateway.TAP
    assert BookingPaymentGateway.normalise("other") == BookingPaymentGateway.OTHER
    assert BookingPaymentGateway.normalise(None) == BookingPaymentGateway.OTHER

    # Through
    assert BookingPaymentThrough.normalise("ushspa") == BookingPaymentThrough.USHSPA
    assert BookingPaymentThrough.normalise("app") == BookingPaymentThrough.USHSPA
    assert BookingPaymentThrough.normalise("ushdesk") == BookingPaymentThrough.USHDESK
    assert BookingPaymentThrough.normalise("desk") == BookingPaymentThrough.USHDESK
    assert BookingPaymentThrough.normalise("pos") == BookingPaymentThrough.USHDESK
    assert BookingPaymentThrough.normalise("other") == BookingPaymentThrough.OTHER
    assert BookingPaymentThrough.normalise(None) == BookingPaymentThrough.OTHER


def test_create_booking_request_schema():
    """Test CreateBookingRequest validates and normalises payment and user fields."""
    req_dict = {
        "customerId": str(uuid.uuid4()),
        "serviceId": str(uuid.uuid4()),
        "therapistId": str(uuid.uuid4()),
        "appointment_date": "2026-10-01",
        "appointment_time": "14:00",
        "paymentId": "PAY-12345",
        "paymentProvider": "myfatoorah",
        "paymentGateway": "knet",
        "paymentThrough": "ushspa",
        "paymentMethod": "KNET",
        "paymentUrl": "https://payment.example.com/pay/123",
        "paymentData": {"invoiceId": "12345"},
        "createdByUserId": "user-uuid-123",
        "createdByUserData": {"name": "Admin Tester", "role": "admin"},
    }
    req = CreateBookingRequest(**req_dict)
    assert req.payment_id == "PAY-12345"
    assert req.payment_provider == "MyFatoorah"
    assert req.payment_gateway == "KNET"
    assert req.payment_through == "ushspa"
    assert req.payment_method == "KNET"
    assert req.payment_url == "https://payment.example.com/pay/123"
    assert req.payment_data == {"invoiceId": "12345"}
    assert req.created_by_user == "user-uuid-123"
    assert req.created_by == "user-uuid-123"
    assert req.created_by_user_data == {"name": "Admin Tester", "role": "admin"}


def test_update_booking_request_schema():
    """Test UpdateBookingRequest accepts new payment and user fields."""
    req = UpdateBookingRequest(
        payment_id="PAY-999",
        payment_provider="paymentlink",
        payment_gateway="tap",
        payment_through="ushdesk",
        payment_method="VISA",
        payment_url="https://pay.link/999",
        created_by="user-456",
        created_by_user_data={"id": "user-456", "name": "Desk Staff"},
    )
    assert req.payment_id == "PAY-999"
    assert req.payment_provider == "PaymentLink"
    assert req.payment_gateway == "TAP"
    assert req.payment_through == "ushdesk"
    assert req.payment_method == "VISA"
    assert req.payment_url == "https://pay.link/999"
    assert req.created_by_user == "user-456"
    assert req.created_by == "user-456"
    assert req.created_by_user_data == {"id": "user-456", "name": "Desk Staff"}


def test_update_booking_status_request_schema():
    """Test UpdateBookingStatusRequest accepts payment fields."""
    req = UpdateBookingStatusRequest(
        status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.SUCCESS,
        payment_id="PAY-CONFIRM",
        payment_provider="Deema",
        payment_gateway="Other",
        payment_through="other",
        payment_method="Split",
        payment_url="https://pay.link/done",
    )
    assert req.payment_id == "PAY-CONFIRM"
    assert req.payment_provider == "Deema"
    assert req.payment_gateway == "Other"
    assert req.payment_through == "other"
    assert req.payment_method == "Split"


def test_booking_model_created_by_backward_compat():
    """Test Booking model created_by property forwards to created_by_user."""
    booking = Booking(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        therapist_id=uuid.uuid4(),
        appointment_date=datetime.now(timezone.utc),
        appointment_start=datetime.now(timezone.utc),
        appointment_end=datetime.now(timezone.utc),
        duration_minutes=60,
        total_amount=Decimal("25.000"),
        created_by_user="user-123",
        created_by_user_data={"name": "Alice"},
        payment_id="PAY-001",
        payment_provider="MyFatoorah",
        payment_gateway="KNET",
        payment_through="ushspa",
        payment_method="KNET",
        payment_url="https://fatoorah.com/1",
    )
    assert booking.created_by == "user-123"
    booking.created_by = "user-456"
    assert booking.created_by_user == "user-456"
    assert booking.payment_id == "PAY-001"
    assert booking.payment_provider == "MyFatoorah"
    assert booking.payment_gateway == "KNET"
    assert booking.payment_through == "ushspa"
    assert booking.payment_method == "KNET"
    assert booking.payment_url == "https://fatoorah.com/1"


def test_booking_response_mappers_include_payment_and_user():
    """Test _booking_to_list_item and _booking_to_detail include all new fields."""
    booking = Booking(
        id=uuid.uuid4(),
        booking_number="B261001001",
        customer_id=uuid.uuid4(),
        customer_data={"first_name": "Test", "phone_number": "12345678"},
        service_id=uuid.uuid4(),
        service_data={"name": "Massage", "is_eligible_for_loyalty": True},
        therapist_id=uuid.uuid4(),
        therapist_data={"first_name": "Therapist 1"},
        appointment_date=datetime.now(timezone.utc),
        appointment_start=datetime.now(timezone.utc),
        appointment_end=datetime.now(timezone.utc),
        duration_minutes=60,
        arrangement_price=Decimal("30.000"),
        total_amount=Decimal("30.000"),
        currency="KWD",
        status=BookingStatus.CONFIRMED.value,
        payment_status=PaymentStatus.SUCCESS.value,
        payment_id="PAY-TEST-99",
        payment_provider="MyFatoorah",
        payment_gateway="KNET",
        payment_through="ushspa",
        payment_method="KNET",
        payment_url="https://pay.me/test",
        payment_data={"invoiceId": "99"},
        created_by_user="creator-uuid",
        created_by_user_data={"name": "Creator User", "image": "https://img.com/avatar.png"},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    list_item = _booking_to_list_item(booking)
    assert list_item.payment_id == "PAY-TEST-99"
    assert list_item.payment_provider == "MyFatoorah"
    assert list_item.payment_gateway == "KNET"
    assert list_item.payment_through == "ushspa"
    assert list_item.payment_method == "KNET"
    assert list_item.payment_url == "https://pay.me/test"
    assert list_item.created_by_user == "creator-uuid"
    assert list_item.created_by == "creator-uuid"
    assert list_item.created_by_user_data == {"name": "Creator User", "image": "https://img.com/avatar.png"}

    detail = _booking_to_detail(booking)
    assert detail.payment_id == "PAY-TEST-99"
    assert detail.payment_provider == "MyFatoorah"
    assert detail.payment_gateway == "KNET"
    assert detail.payment_through == "ushspa"
    assert detail.payment_method == "KNET"
    assert detail.payment_url == "https://pay.me/test"
    assert detail.created_by_user == "creator-uuid"
    assert detail.created_by == "creator-uuid"
    assert detail.created_by_user_data == {"name": "Creator User", "image": "https://img.com/avatar.png"}


@pytest.mark.asyncio
async def test_create_booking_endpoint_passes_payment_and_user_fields():
    """Test that create_booking handler forwards payment and creator fields to service."""
    mock_service = AsyncMock()
    mock_ushauth = AsyncMock()
    mock_ushauth.get_customer_profile.return_value = {
        "id": "11111111-1111-1111-1111-111111111111",
        "first_name": "John",
        "last_name": "Doe",
        "phone_number": "96512345678",
        "email": "john@example.com",
    }
    mock_ushauth.get_service.return_value = {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "Full Body Massage",
        "duration_minutes": 60,
        "price": "25.000",
        "is_eligible_for_loyalty": True,
    }
    mock_ushauth.get_service_arrangement.return_value = {
        "id": "33333333-3333-3333-3333-333333333333",
        "price": "25.000",
        "arrangement_services": [],
    }
    mock_ushauth.get_therapist.return_value = {
        "id": "44444444-4444-4444-4444-444444444444",
        "first_name": "Jane",
        "last_name": "Therapist",
    }
    mock_ushauth.check_appointment_availability.return_value = {
        "available": "yes",
        "therapist_id": "44444444-4444-4444-4444-444444444444",
        "available_therapist_ids": ["44444444-4444-4444-4444-444444444444"],
    }

    mock_booking = MagicMock(spec=Booking)
    mock_booking.id = uuid.uuid4()
    mock_booking.booking_number = "B261001002"
    mock_booking.customer_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    mock_booking.total_amount = Decimal("25.000")
    mock_booking.status = "confirmed"
    mock_booking.payment_status = "success"
    mock_booking.payment_id = "PAY-INVOICE-123"
    mock_booking.payment_provider = "MyFatoorah"
    mock_booking.payment_gateway = "KNET"
    mock_booking.payment_through = "ushspa"
    mock_booking.payment_method = "KNET"
    mock_booking.payment_url = "https://fatoorah.com/invoice/123"
    mock_booking.payment_type = "service"
    mock_booking.payment_data = {"invoiceId": "123"}
    mock_booking.service_data = {"is_eligible_for_loyalty": True}
    mock_booking.loyalty_data = None
    mock_booking.reward_id = None
    mock_booking.voucher_id = None
    mock_booking.voucher_data = None
    mock_booking.created_by_user = "user-requester-sub"
    mock_booking.created_by_user_data = {"name": "Requester"}
    mock_service.create_booking.return_value = mock_booking

    request = MagicMock()
    body = CreateBookingRequest(
        customer_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        service_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        service_arrangement_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        therapist_id=uuid.UUID("44444444-4444-4444-4444-444444444444"),
        appointment_date="2026-10-01",
        appointment_time="10:00:00",
        payment_id="PAY-INVOICE-123",
        payment_provider="MyFatoorah",
        payment_gateway="KNET",
        payment_through="ushspa",
        payment_method="KNET",
        payment_url="https://fatoorah.com/invoice/123",
        payment_data={"invoiceId": "123"},
        created_by_user="user-requester-sub",
        created_by_user_data={"name": "Requester"},
    )
    current_user = MagicMock()
    current_user.sub = "user-requester-sub"
    current_user.first_name = "Requester"
    current_user.last_name = "User"
    current_user.phone_number = "96599999999"
    current_user.email = "req@test.com"
    current_user.role = "admin"
    settings = MagicMock()

    res = await create_booking(
        request=request,
        body=body,
        current_user=current_user,
        booking_service=mock_service,
        ushauth=mock_ushauth,
        settings=settings,
    )

    assert res.success is True
    assert res.data.payment_id == "PAY-INVOICE-123"
    assert res.data.payment_provider == "MyFatoorah"
    assert res.data.payment_gateway == "KNET"
    assert res.data.payment_through == "ushspa"
    assert res.data.payment_method == "KNET"
    assert res.data.payment_url == "https://fatoorah.com/invoice/123"
    assert res.data.created_by_user == "user-requester-sub"
    assert res.data.created_by == "user-requester-sub"

    # Verify kwargs passed to service
    _, kwargs = mock_service.create_booking.call_args
    assert kwargs["payment_id"] == "PAY-INVOICE-123"
    assert kwargs["payment_provider"] == "MyFatoorah"
    assert kwargs["payment_gateway"] == "KNET"
    assert kwargs["payment_through"] == "ushspa"
    assert kwargs["payment_method"] == "KNET"
    assert kwargs["payment_url"] == "https://fatoorah.com/invoice/123"
    assert kwargs["created_by_user"] == "user-requester-sub"
    assert kwargs["created_by_user_data"] == {"name": "Requester"}


@pytest.mark.asyncio
async def test_update_booking_endpoint_passes_payment_and_user_fields():
    """Test that update_booking handler forwards payment and creator fields to service."""
    booking_id = uuid.uuid4()
    mock_service = AsyncMock()
    mock_ushauth = AsyncMock()

    mock_existing = MagicMock(spec=Booking)
    mock_existing.id = booking_id
    mock_existing.customer_id = uuid.uuid4()
    mock_service.get_booking.return_value = mock_existing

    mock_updated = MagicMock(spec=Booking)
    mock_updated.id = booking_id
    mock_updated.customer_id = mock_existing.customer_id
    mock_updated.customer_data = {"first_name": "Test"}
    mock_updated.service_id = uuid.uuid4()
    mock_updated.service_data = {}
    mock_updated.therapist_id = uuid.uuid4()
    mock_updated.therapist_data = {}
    mock_updated.appointment_date = datetime.now(timezone.utc)
    mock_updated.appointment_start = datetime.now(timezone.utc)
    mock_updated.appointment_end = datetime.now(timezone.utc)
    mock_updated.duration_minutes = 60
    mock_updated.extra_minutes = 0
    mock_updated.total_duration = 60
    mock_updated.arrangement_price = Decimal("20.000")
    mock_updated.price_for_extra_minutes = Decimal("0.000")
    mock_updated.addon_price = Decimal("0.000")
    mock_updated.discount = Decimal("0.000")
    mock_updated.tax = Decimal("0.000")
    mock_updated.fees = Decimal("0.000")
    mock_updated.total_amount = Decimal("20.000")
    mock_updated.currency = "KWD"
    mock_updated.status = "confirmed"
    mock_updated.payment_status = "success"
    mock_updated.payment_id = "PAY-UPDATED-555"
    mock_updated.payment_provider = "PaymentLink"
    mock_updated.payment_gateway = "TAP"
    mock_updated.payment_through = "ushdesk"
    mock_updated.payment_method = "KNET"
    mock_updated.payment_url = "https://pay.link/555"
    mock_updated.payment_data = {"status": "Paid"}
    mock_updated.created_by_user = "user-updater"
    mock_updated.created_by_user_data = {"name": "Desk Worker"}
    mock_updated.addons = []
    mock_updated.customer_notes = None
    mock_updated.internal_notes = None
    mock_updated.status_history = []
    mock_updated.loyalty_data = None
    mock_updated.reward_id = None
    mock_updated.voucher_id = None
    mock_updated.voucher_data = None
    mock_updated.created_at = datetime.now(timezone.utc)
    mock_updated.updated_at = datetime.now(timezone.utc)
    mock_service.update_booking.return_value = mock_updated

    current_user = MagicMock()
    current_user.sub = str(uuid.uuid4())
    body = UpdateBookingRequest(
        payment_id="PAY-UPDATED-555",
        payment_provider="paymentlink",
        payment_gateway="tap",
        payment_through="ushdesk",
        payment_method="KNET",
        payment_url="https://pay.link/555",
        created_by_user="user-updater",
        created_by_user_data={"name": "Desk Worker"},
        source="admin",
    )

    res = await update_booking(
        booking_id=booking_id,
        body=body,
        current_user=current_user,
        booking_service=mock_service,
        ushauth=mock_ushauth,
    )

    import json
    content = json.loads(res.body.decode())
    assert content["success"] is True
    data = content["data"]
    assert data["payment_id"] == "PAY-UPDATED-555"
    assert data["payment_provider"] == "PaymentLink"
    assert data["payment_gateway"] == "TAP"
    assert data["payment_through"] == "ushdesk"
    assert data["payment_method"] == "KNET"
    assert data["payment_url"] == "https://pay.link/555"
    assert data["created_by_user"] == "user-updater"
    assert data["created_by"] == "user-updater"
    assert data["created_by_user_data"] == {"name": "Desk Worker"}

    # Verify kwargs passed to service
    _, kwargs = mock_service.update_booking.call_args
    assert kwargs["payment_id"] == "PAY-UPDATED-555"
    assert kwargs["payment_provider"] == "PaymentLink"
    assert kwargs["payment_gateway"] == "TAP"
    assert kwargs["payment_through"] == "ushdesk"
    assert kwargs["payment_method"] == "KNET"
    assert kwargs["payment_url"] == "https://pay.link/555"
    assert kwargs["created_by_user"] == "user-updater"
    assert kwargs["created_by_user_data"] == {"name": "Desk Worker"}
