"""
tests/unit/test_events.py
─────────────────────────
Unit tests for SQS event contracts and event_type payload serialization.
"""

import json
from app.events.contracts import (
    BookingCreatedEvent,
    BookingRequestedEvent,
    BookingConfirmedEvent,
    BookingCancelledEvent,
    BookingStatusUpdatedEvent,
    LoyaltyRewardedEvent,
    LoyaltyRedeemedEvent,
)


def test_booking_requested_event_payload():
    """Verify Booking.Requested event produces event_type='booking.requested' in payload."""
    event = BookingRequestedEvent(
        booking_id="11111111-1111-1111-1111-111111111111",
        customer_id="22222222-2222-2222-2222-222222222222",
        customer_name="Test Customer",
        status="requested",
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Booking.Requested"
    assert payload["event_type"] == "booking.requested"
    assert payload["booking_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["customer_name"] == "Test Customer"
    assert "event_id" in payload
    assert "occurred_at" in payload


def test_booking_created_event_payload():
    """Verify Booking.Created event produces event_type='booking.created' in payload."""
    event = BookingCreatedEvent(
        booking_id="33333333-3333-3333-3333-333333333333",
        customer_id="44444444-4444-4444-4444-444444444444",
        customer_name="Test Customer",
        status="requested",
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Booking.Created"
    assert payload["event_type"] == "booking.created"
    assert payload["booking_id"] == "33333333-3333-3333-3333-333333333333"


def test_booking_confirmed_event_payload():
    """Verify Booking.Confirmed event produces event_type='booking.confirmed' with new fields."""
    event = BookingConfirmedEvent(
        booking_id="55555555-5555-5555-5555-555555555555",
        customer_id="66666666-6666-6666-6666-666666666666",
        service_arrangement_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        customer_name="Test Customer",
        customer_email="test@example.com",
        appointment_date="2026-08-25",
        appointment_starttime="09:00",
        appointment_endtime="10:00",
        payments_meta={
            "payment_id": "100623810000000483",
            "invoice_id": "7106562",
            "transaction_id": "623810001265311",
            "is_paid": True,
            "payment_gateway": "KNET",
        },
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Booking.Confirmed"
    assert payload["event_type"] == "booking.confirmed"
    assert payload["booking_id"] == "55555555-5555-5555-5555-555555555555"
    assert payload["service_arrangement_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert payload["appointment_date"] == "2026-08-25"
    assert payload["appointment_starttime"] == "09:00"
    assert payload["appointment_endtime"] == "10:00"
    assert payload["customer_email"] == "test@example.com"
    assert payload["payments_meta"]["payment_id"] == "100623810000000483"
    assert payload["payments_meta"]["is_paid"] is True
    assert payload["payments_meta"]["payment_gateway"] == "KNET"
    # is_eligible_for_loyalty must default to False and be present in payload
    assert "is_eligible_for_loyalty" in payload
    assert payload["is_eligible_for_loyalty"] is False

    # Verify it can be set to True
    eligible_event = BookingConfirmedEvent(
        booking_id="55555555-5555-5555-5555-555555555555",
        is_eligible_for_loyalty=True,
    )
    eligible_payload = json.loads(eligible_event.to_json())
    assert eligible_payload["is_eligible_for_loyalty"] is True


def test_booking_cancelled_event_payload():
    """Verify Booking.Cancelled event includes scheduling and service arrangement fields."""
    event = BookingCancelledEvent(
        booking_id="77777777-7777-7777-7777-777777777777",
        customer_id="88888888-8888-8888-8888-888888888888",
        service_arrangement_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        therapist_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        appointment_date="2026-08-25",
        appointment_starttime="14:00",
        appointment_endtime="15:00",
        customer_name="Test Customer",
        cancellation_reason="Customer request",
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Booking.Cancelled"
    assert payload["event_type"] == "booking.cancelled"
    assert payload["booking_id"] == "77777777-7777-7777-7777-777777777777"
    assert payload["service_arrangement_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert payload["appointment_date"] == "2026-08-25"
    assert payload["appointment_starttime"] == "14:00"
    assert payload["appointment_endtime"] == "15:00"
    assert payload["cancellation_reason"] == "Customer request"


def test_booking_payment_pending_event_payload():
    """Verify Booking.PaymentPending produces event_type='booking.payment_pending' with all details."""
    from app.events.contracts import BookingPaymentPendingEvent

    event = BookingPaymentPendingEvent(
        booking_id="99999999-9999-9999-9999-999999999999",
        booking_reference="INV-9999",
        customer_id="88888888-8888-8888-8888-888888888888",
        customer_name="Fatima Al-Sabah",
        customer_phone="+96590000000",
        customer_email="fatima@example.com",
        branch_id="11111111-1111-1111-1111-111111111111",
        branch_name="Salmiya Branch",
        service_id="22222222-2222-2222-2222-222222222222",
        service_name="Deep Tissue Massage",
        service_arrangement_id="33333333-3333-3333-3333-333333333333",
        service_arrangement_name="VIP Suite 1",
        therapist_id="44444444-4444-4444-4444-444444444444",
        therapist_name="Amina Khan",
        appointment_start="2026-08-30T10:00:00+00:00",
        appointment_end="2026-08-30T11:00:00+00:00",
        appointment_date="2026-08-30",
        appointment_starttime="10:00",
        appointment_endtime="11:00",
        appointment_time="10:00",
        duration_minutes=60,
        extra_minutes=15,
        total_duration=75,
        booking_type="branch",
        status="payment_pending",
        payment_status="pending",
        total_amount="35.000",
        currency="KWD",
        pricing={"total": "35.000", "currency": "KWD"},
        payments_meta={"invoice_id": "7106562"},
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Booking.PaymentPending"
    assert payload["event_type"] == "booking.payment_pending"
    assert payload["booking_id"] == "99999999-9999-9999-9999-999999999999"
    assert payload["booking_reference"] == "INV-9999"
    assert payload["customer_name"] == "Fatima Al-Sabah"
    assert payload["branch_name"] == "Salmiya Branch"
    assert payload["service_name"] == "Deep Tissue Massage"
    assert payload["service_arrangement_name"] == "VIP Suite 1"
    assert payload["therapist_name"] == "Amina Khan"
    assert payload["status"] == "payment_pending"
    assert payload["payment_status"] == "pending"
    assert payload["total_amount"] == "35.000"
    assert payload["total_duration"] == 75


def test_booking_completed_event_payload():
    """Verify Booking.Completed produces event_type='booking.completed' with all details."""
    from app.events.contracts import BookingCompletedEvent

    event = BookingCompletedEvent(
        booking_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        booking_reference="INV-AAAA",
        customer_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        customer_name="Sara Ahmed",
        customer_phone="+96591111111",
        customer_email="sara@example.com",
        branch_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        branch_name="Kuwait City Branch",
        service_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        service_name="Swedish Massage",
        service_arrangement_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        service_arrangement_name="Room 3",
        therapist_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        therapist_name="Elena Rostova",
        appointment_start="2026-08-30T14:00:00+00:00",
        appointment_end="2026-08-30T15:00:00+00:00",
        appointment_date="2026-08-30",
        appointment_starttime="14:00",
        appointment_endtime="15:00",
        duration_minutes=60,
        total_duration=60,
        booking_type="branch",
        status="completed",
        payment_status="paid",
        total_amount="40.000",
        currency="KWD",
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Booking.Completed"
    assert payload["event_type"] == "booking.completed"
    assert payload["booking_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert payload["customer_name"] == "Sara Ahmed"
    assert payload["branch_name"] == "Kuwait City Branch"
    assert payload["service_name"] == "Swedish Massage"
    assert payload["therapist_name"] == "Elena Rostova"
    assert payload["status"] == "completed"
    assert payload["total_amount"] == "40.000"


def test_loyalty_rewarded_event_payload():
    """Verify Loyalty.Rewarded event produces event_type='loyalty.rewarded' in payload."""
    event = LoyaltyRewardedEvent(
        reward_id="r1111111-1111-1111-1111-111111111111",
        tracker_id="t2222222-2222-2222-2222-222222222222",
        customer_id="c3333333-3333-3333-3333-333333333333",
        customer_name="Fatima Al-Mansoor",
        customer_phone="+96590001122",
        customer_email="fatima@example.com",
        service_id="s4444444-4444-4444-4444-444444444444",
        service_name="Deep Tissue Massage",
        bookings_required=5,
        total_rewards_earned=1,
        reward_status="available",
        expires_at="2026-09-10T12:00:00+00:00",
        booking_id="b5555555-5555-5555-5555-555555555555",
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Loyalty.Rewarded"
    assert payload["event_type"] == "loyalty.rewarded"
    assert payload["reward_id"] == "r1111111-1111-1111-1111-111111111111"
    assert payload["customer_name"] == "Fatima Al-Mansoor"
    assert payload["service_name"] == "Deep Tissue Massage"
    assert payload["bookings_required"] == 5
    assert payload["total_rewards_earned"] == 1
    assert payload["reward_status"] == "available"


def test_loyalty_redeemed_event_payload():
    """Verify Loyalty.Redeemed event produces event_type='loyalty.redeedmed' with all reward response data."""
    reward_data = {
        "id": "e405581a-1a5c-4be7-a76e-01213ebc7640",
        "customer_id": "b92c374d-fdb5-48ff-96d5-bb4dc2abc452",
        "service_id": "d104457d-98f6-49cf-afa3-c304fdd51ea4",
        "service_arrangement_id": "dab7e167-7f57-4e30-984b-ba45f88c95f6",
        "service_name": "24K Gold Luxury Rejuvenating Facial",
        "status": "redeemed",
        "earned_from_booking_id": "496a35f6-3b99-4a93-9f53-5ce98125d680",
        "redeemed_in_booking_id": "e405581a-1a5c-4be7-a76e-01213ebc7640",
        "redeemed_at": "2026-08-31T16:21:21.614703Z",
        "expires_at": "2026-09-10T16:06:53.486752Z",
        "created_at": "2026-08-31T16:06:53.481671Z",
        # Booking context
        "therapist_id": "aaa00000-0000-0000-0000-000000000001",
        "appointment_date": "2026-09-01",
        "appointment_time": "10:30",
        "duration": 60,
    }
    event = LoyaltyRedeemedEvent(
        reward=reward_data,
        id=reward_data["id"],
        reward_id=reward_data["id"],
        customer_id=reward_data["customer_id"],
        service_id=reward_data["service_id"],
        service_arrangement_id=reward_data["service_arrangement_id"],
        service_name=reward_data["service_name"],
        status=reward_data["status"],
        earned_from_booking_id=reward_data["earned_from_booking_id"],
        redeemed_in_booking_id=reward_data["redeemed_in_booking_id"],
        redeemed_at=reward_data["redeemed_at"],
        expires_at=reward_data["expires_at"],
        created_at=reward_data["created_at"],
        therapist_id=reward_data["therapist_id"],
        appointment_date=reward_data["appointment_date"],
        appointment_time=reward_data["appointment_time"],
        duration=reward_data["duration"],
    )
    payload = json.loads(event.to_json())

    assert payload["event_name"] == "Loyalty.Redeemed"
    assert payload["event_type"] == "loyalty.redeedmed"
    assert payload["reward"] == reward_data
    assert payload["reward"]["id"] == "e405581a-1a5c-4be7-a76e-01213ebc7640"
    assert payload["reward"]["customer_id"] == "b92c374d-fdb5-48ff-96d5-bb4dc2abc452"
    assert payload["reward"]["service_name"] == "24K Gold Luxury Rejuvenating Facial"
    assert payload["reward"]["status"] == "redeemed"
    assert payload["reward"]["redeemed_at"] == "2026-08-31T16:21:21.614703Z"
    assert payload["reward"]["therapist_id"] == "aaa00000-0000-0000-0000-000000000001"
    assert payload["reward"]["appointment_date"] == "2026-09-01"
    assert payload["reward"]["appointment_time"] == "10:30"
    assert payload["reward"]["duration"] == 60
    assert payload["id"] == "e405581a-1a5c-4be7-a76e-01213ebc7640"
    assert payload["customer_id"] == "b92c374d-fdb5-48ff-96d5-bb4dc2abc452"
    assert payload["therapist_id"] == "aaa00000-0000-0000-0000-000000000001"
    assert payload["appointment_date"] == "2026-09-01"
    assert payload["appointment_time"] == "10:30"
    assert payload["duration"] == 60
    assert "data" not in payload  # data field removed — no duplication
    assert "event_id" in payload
    assert "occurred_at" in payload
