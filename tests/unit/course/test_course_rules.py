"""Pure course rules: price, balance, effective status, reservation validation, refund guard."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.booking.infrastructure.models import Booking
from app.course.domain import rules
from app.course.domain.value_objects import CourseError

from tests.unit.course.helpers import SERVICE_ID, course_payload, make_sub, now


def _svc(service_id=SERVICE_ID, arrangement_type="vip_suite"):
    return [{"service_id": str(service_id), "service_arrangement_data": {"arrangement_type": arrangement_type}}]


def _validate(sub, **kw):
    params = {
        "customer_id": sub.customer_id,
        "services": _svc(),
        "appointment_start": now() + timedelta(days=1),
    }
    params.update(kw)
    rules.validate_reservation(sub, **params)


# ── pricing ──────────────────────────────────────────────────────────────────


def test_price_auto_is_unit_price_times_sessions():
    assert rules.compute_course_price(course_payload(price="0.000")) == Decimal("100.000")


def test_price_manual_wins_and_is_quantized():
    assert rules.compute_course_price(course_payload(price="85.5")) == Decimal("85.500")


def test_price_falls_back_to_effective_price_without_unit():
    payload = course_payload(price="0", unit_price=None, service={}, effective_price="42.0004")
    assert rules.compute_course_price(payload) == Decimal("42.000")


def test_expires_at_counts_from_activation():
    t = now()
    assert rules.compute_expires_at(t, 90) == t + timedelta(days=90)


# ── balance / status ─────────────────────────────────────────────────────────


def test_available_and_redeemable():
    sub = make_sub(used_sessions=2, reserved_sessions=1)
    assert rules.available_sessions(sub) == 2
    assert rules.is_redeemable(sub)


def test_effective_status_expired_when_past_expiry():
    sub = make_sub(expires_at=now() - timedelta(seconds=1))
    assert sub.status == "active"
    assert rules.effective_status(sub) == "expired"
    assert not rules.is_redeemable(sub)


def test_not_redeemable_when_no_sessions_left():
    sub = make_sub(used_sessions=3, reserved_sessions=2)
    assert rules.available_sessions(sub) == 0
    assert not rules.is_redeemable(sub)


# ── reservation validation (contract E) ──────────────────────────────────────


def test_valid_reservation_passes():
    _validate(make_sub())


@pytest.mark.parametrize(
    "overrides, kwargs, code",
    [
        ({"expires_at": now() - timedelta(minutes=1)}, {}, "COURSE_SUBSCRIPTION_EXPIRED"),
        ({"status": "expired"}, {}, "COURSE_SUBSCRIPTION_EXPIRED"),
        ({"status": "payment_pending"}, {}, "COURSE_SUBSCRIPTION_NOT_ACTIVE"),
        ({"status": "exhausted"}, {}, "COURSE_SUBSCRIPTION_NOT_ACTIVE"),
        ({"used_sessions": 4, "reserved_sessions": 1}, {}, "COURSE_NO_SESSIONS_AVAILABLE"),
        ({}, {"customer_id": uuid.uuid4()}, "COURSE_CUSTOMER_MISMATCH"),
        ({}, {"services": _svc(service_id=uuid.uuid4())}, "COURSE_SERVICE_MISMATCH"),
        ({}, {"services": _svc() + _svc()}, "COURSE_SERVICE_MISMATCH"),
        ({}, {"services": _svc(arrangement_type="single_room")}, "COURSE_ARRANGEMENT_MISMATCH"),
        ({}, {"arrangement_type": "couple_room"}, "COURSE_ARRANGEMENT_MISMATCH"),
        ({}, {"addons": [{"addon_id": "a"}]}, "COURSE_EXTRAS_NOT_ALLOWED"),
        ({}, {"extra_minutes": 15}, "COURSE_EXTRAS_NOT_ALLOWED"),
        ({}, {"sessions": [{"addons": []}, {"addons": []}]}, "COURSE_SINGLE_SESSION_ONLY"),
        ({}, {"sessions": [{"addons": [{"id": "x"}], "extra_minutes": 0}]}, "COURSE_EXTRAS_NOT_ALLOWED"),
    ],
)
def test_reservation_rejections(overrides, kwargs, code):
    sub = make_sub(**overrides)
    with pytest.raises(CourseError) as exc:
        _validate(sub, **kwargs)
    assert exc.value.code == code


def test_appointment_after_expiry_rejected():
    sub = make_sub(expires_at=now() + timedelta(days=3))
    with pytest.raises(CourseError) as exc:
        _validate(sub, appointment_start=now() + timedelta(days=3, minutes=1))
    assert exc.value.code == "COURSE_APPOINTMENT_AFTER_EXPIRY"
    assert exc.value.status_code == 400


def test_error_http_statuses():
    assert CourseError("COURSE_SUBSCRIPTION_NOT_FOUND").status_code == 404
    assert CourseError("COURSE_NO_SESSIONS_AVAILABLE").status_code == 409
    assert CourseError("COURSE_REFUND_NOT_ALLOWED").status_code == 409
    assert CourseError("COURSE_SERVICE_MISMATCH").status_code == 400


# ── refund guard (rule A5) ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides, allowed",
    [
        ({}, True),
        ({"status": "expired"}, True),
        ({"used_sessions": 1}, False),
        ({"reserved_sessions": 1}, False),
        ({"status": "exhausted", "used_sessions": 5}, False),
        ({"status": "payment_pending"}, False),
        ({"status": "refunded"}, False),
    ],
)
def test_can_refund(overrides, allowed):
    assert rules.can_refund(make_sub(**overrides)) is allowed


# ── misc helpers ─────────────────────────────────────────────────────────────


def test_is_course_booking_is_safe_on_mocks():
    assert rules.is_course_booking(MagicMock(spec=Booking)) is False
    assert rules.is_course_booking(MagicMock()) is False
    assert rules.is_course_booking(None) is False
    b = MagicMock(spec=Booking)
    b.booking_type = "course_redemption"
    assert rules.is_course_booking(b) is True
    b2 = MagicMock(spec=Booking)
    b2.course_subscription_id = uuid.uuid4()
    assert rules.is_course_booking(b2) is True


def test_next_session_number_reuses_released_gap():
    assert rules.next_session_number([]) == 1
    assert rules.next_session_number([1, 2, 3]) == 4
    assert rules.next_session_number([1, 3]) == 2
