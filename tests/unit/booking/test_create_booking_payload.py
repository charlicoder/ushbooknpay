"""
tests/unit/booking/test_create_booking_payload.py
─────────────────────────────────────────────────
Unit tests for CreateBookingRequest schema and payload normalization.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.booking.interfaces.schemas import CreateBookingRequest


def test_create_booking_request_payload_parsing():
    """Verify that CreateBookingRequest correctly parses the full payload format."""
    payload = {
        "service_id": "de9d93d5-cbf8-41c9-98a4-a2a261bbb1b1",
        "service_name": "Authentic Turkish Foam Bath & Steam",
        "service_category": "WELLNESS THERAPY",
        "base_price": "40.00",
        "baseDuration": 60,
        "branch_id": "c53023e8-0ed8-45ba-8eda-e4c3b522e497",
        "branch_data": {
            "branch_name": "Al Khiran Coastal Retreat",
            "branch_address": "Al Khiran",
        },
        "service_arrangement_id": "5f414197-2fe7-45c1-97ed-7e802525832e",
        "service_arrangement_data": {
            "arrangement_name": "Imperial Presidential Sanctuary",
            "arrangement_type": "vip_suite",
        },
        "therapist_id": "fc1c88d1-7f95-49e4-a073-1051f20d1620",
        "therapist_data": {
            "therapist_name": "Dana",
        },
        "selected_addons": [
            {
                "id": "75248505-7916-440c-8e62-e902f31e516a",
                "name": "Hydrating Collagen Eye & Lip Mask",
                "description": "Intensive peptide patches reducing puffiness and fine lines.",
                "price": "6.000",
                "currency": "KWD",
                "is_active": True,
            }
        ],
        "addons_duration": 15,
        "extra_minutes": 0,
        "extra_price": "0.00",
        "date": "2026-08-26",
        "formattedDate": "Aug 26, 2026",
        "time_slot": "13:00",
        "displayTime": "1:00 PM",
        "customerMessage": "",
        "customer_notes": "",
        "pricing_details": {
            "base": "40.00",
            "base_price": "40.00",
            "arrangement": "40.00",
            "arrangement_price": "40.00",
            "addons": "6.00",
            "addons_price": "6.00",
            "extratime": "0.00",
            "extra_time_price": "0.00",
            "subtotal": "46.00",
            "total": "46.00",
            "total_price": "46.00",
            "currency": "KWD",
        },
        "total_price": "46.00",
        "total_duration": 75,
        "currency": "KWD",
    }

    req = CreateBookingRequest(**payload)

    assert req.service_id == uuid.UUID("de9d93d5-cbf8-41c9-98a4-a2a261bbb1b1")
    assert req.service_name == "Authentic Turkish Foam Bath & Steam"
    assert req.service_category == "WELLNESS THERAPY"
    assert req.base_price == "40.00"
    assert req.base_duration == 60
    assert req.branch_id == uuid.UUID("c53023e8-0ed8-45ba-8eda-e4c3b522e497")
    assert req.branch_data["branch_name"] == "Al Khiran Coastal Retreat"
    assert req.service_arrangement_id == uuid.UUID("5f414197-2fe7-45c1-97ed-7e802525832e")
    assert req.service_arrangement_data["arrangement_name"] == "Imperial Presidential Sanctuary"
    assert req.therapist_id == uuid.UUID("fc1c88d1-7f95-49e4-a073-1051f20d1620")
    assert req.therapist_data["therapist_name"] == "Dana"
    assert len(req.selected_addons) == 1
    assert req.selected_addons[0]["id"] == "75248505-7916-440c-8e62-e902f31e516a"
    assert req.addons_duration == 15
    assert req.date == "2026-08-26"
    assert req.formatted_date == "Aug 26, 2026"
    assert req.time_slot == "13:00"
    assert req.display_time == "1:00 PM"
    assert req.pricing_details["total_price"] == "46.00"
    assert req.total_price == "46.00"
    assert req.total_duration == 75
    assert req.currency == "KWD"




def test_booking_payments_meta_schema():
    """Verify that CreateBookingRequest and BookingDetailResponse support payments_meta."""
    from app.booking.interfaces.schemas import CreateBookingRequest, BookingDetailResponse, PricingBreakdownSchema

    payload = {
        "service_arrangement_id": "5f414197-2fe7-45c1-97ed-7e802525832e",
        "payments_meta": {
            "payment_id": "100623710000000606",
            "invoice_id": "7103271",
            "transaction_id": "623710001297726",
            "is_paid": True,
            "status": "success",
            "payment_gateway": "KNET",
            "invoice_value": "46.000",
        },
    }

    req = CreateBookingRequest(**payload)
    assert req.payments_meta["payment_id"] == "100623710000000606"
    assert req.payments_meta["invoice_id"] == "7103271"
    assert req.payments_meta["is_paid"] is True
    assert req.payments_meta["payment_gateway"] == "KNET"


def test_update_booking_status_request_payload_with_payments_meta():
    """Verify that UpdateBookingStatusRequest parses the exact payload from user request."""
    from app.booking.interfaces.schemas import UpdateBookingStatusRequest
    from app.booking.domain.value_objects import BookingStatus, PaymentStatus

    payload = {
        "status": "confirmed",
        "payment_status": "success",
        "reason": "Payment Success",
        "source": "ushspa app",
        "payments_meta": {
            "is_paid": True,
            "invoice_id": "7106562",
            "status": "Paid",
            "invoice_reference": "2026194654",
            "customer_reference": "ORDER_1787705231089",
            "created_date": "2026-08-26T03:47:11.397",
            "invoice_value": "52",
            "customer_name": "K Md Mamunur Rashid",
            "customer_mobile": "+96541028983",
            "customer_email": "Mamun1980@gmail.com",
            "transaction_date": "2026-08-26T03:47:30.4066667",
            "payment_gateway": "KNET",
            "payment_id": "100623810000000483",
            "transaction_id": "623810001265311",
        },
    }

    req = UpdateBookingStatusRequest(**payload)
    assert req.status == BookingStatus.CONFIRMED
    assert req.payment_status == "success"
    assert req.reason == "Payment Success"
    assert req.source == "ushspa app"
    assert req.payments_meta["invoice_id"] == "7106562"
    assert req.payments_meta["payment_id"] == "100623810000000483"
    assert req.payments_meta["is_paid"] is True


def test_booking_detail_response_includes_appointment_date():
    """Verify that BookingDetailResponse and BookingListItem include appointment_date formatted as YYYY-MM-DD."""
    from datetime import datetime, timezone
    from app.booking.interfaces.schemas import BookingDetailResponse, BookingListItem, PricingBreakdownSchema

    pricing = PricingBreakdownSchema(
        arrangement_price="40.000",
        price_for_extra_minutes="0.000",
        addon_price="5.000",
        discount="0.000",
        tax="0.000",
        fees="0.000",
        total="45.000",
        currency="KWD",
    )

    detail = BookingDetailResponse(
        id="b1111111-1111-1111-1111-111111111111",
        customer_id="c1111111-1111-1111-1111-111111111111",
        service_id="s1111111-1111-1111-1111-111111111111",
        therapist_id="t1111111-1111-1111-1111-111111111111",
        appointment_date=datetime(2026, 9, 6, 0, 0, 0, tzinfo=timezone.utc),
        appointment_start=datetime(2026, 9, 6, 14, 0, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 6, 15, 0, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        status="confirmed",
        payment_status="pending",
        pricing=pricing,
        addons=[],
        created_at=datetime(2026, 9, 3, 0, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 3, 0, 0, 0, tzinfo=timezone.utc),
    )

    dumped = detail.model_dump(mode="json")
    assert dumped["appointment_date"] == "2026-09-06"
    assert detail.appointment_date == "2026-09-06"

    # Also test BookingListItem
    item = BookingListItem(
        id="b1111111-1111-1111-1111-111111111111",
        customer_id="c1111111-1111-1111-1111-111111111111",
        service_id="s1111111-1111-1111-1111-111111111111",
        therapist_id="t1111111-1111-1111-1111-111111111111",
        appointment_date="2026-09-06T00:00:00Z",
        appointment_start=datetime(2026, 9, 6, 14, 0, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 6, 15, 0, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        status="confirmed",
        payment_status="pending",
        total_amount="45.000",
        currency="KWD",
        created_at=datetime(2026, 9, 3, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert item.model_dump(mode="json")["appointment_date"] == "2026-09-06"
