"""A therapist is blocked only for the sessions he/she serves, not for the whole booking."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.booking.domain.sessions import therapist_busy_windows

TZ = timezone(timedelta(hours=3))
A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"


def _dt(h, m=0):
    return datetime(2026, 11, 1, h, m, tzinfo=TZ)


def _booking():
    return SimpleNamespace(
        therapist_id=A,
        secondary_therapist_id=None,
        appointment_start=_dt(13),
        appointment_end=_dt(17),
        sessions=[
            {"therapist_id": A, "appointment_start": _dt(13).isoformat(), "appointment_end": _dt(13, 45).isoformat()},
            {"therapist_id": B, "appointment_start": _dt(15).isoformat(), "appointment_end": _dt(17).isoformat()},
        ],
    )


def test_each_therapist_blocked_only_for_own_session():
    bk = _booking()
    assert therapist_busy_windows(bk, A) == [(_dt(13), _dt(13, 45))]
    assert therapist_busy_windows(bk, B) == [(_dt(15), _dt(17))]


def test_legacy_booking_blocks_full_window():
    bk = SimpleNamespace(
        therapist_id=A, secondary_therapist_id=None,
        appointment_start=_dt(13), appointment_end=_dt(15), sessions=[],
    )
    assert therapist_busy_windows(bk, A) == [(_dt(13), _dt(15))]
    assert therapist_busy_windows(bk, B) == []


import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal
from app.booking.application.services import BookingService
from app.booking.infrastructure.models import Booking


def _make_booking(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        status="confirmed",
        payment_status="success",
        appointment_date=_dt(10),
        appointment_start=_dt(10),
        appointment_end=_dt(11),
        duration_minutes=60,
        extra_minutes=0,
        total_duration=60,
        arrangement_price=Decimal("40"),
        price_for_extra_minutes=Decimal("0"),
        addon_price=Decimal("0"),
        discount=Decimal("0"),
        tax=Decimal("0"),
        fees=Decimal("0"),
        total_amount=Decimal("40"),
        currency="KWD",
        booking_type="branch_service",
        payment_type="service",
        customer_data={},
        branch_data={},
        therapist_data={},
        payment_data={},
        addons=[],
        services=[],
        sessions=[],
        loyalty_data=None,
        internal_notes="",
    )
    defaults.update(kwargs)
    return Booking(**defaults)


@pytest.mark.asyncio
async def test_update_booking_syncs_single_session():
    import uuid
    svc = BookingService(session=AsyncMock(), settings=MagicMock())
    t1 = uuid.UUID(A)
    t2 = uuid.UUID(B)
    booking = _make_booking(
        therapist_id=t1,
        therapist_data={"id": str(t1), "name": "Therapist A"},
        appointment_start=_dt(10),
        appointment_end=_dt(11),
        duration_minutes=60,
        sessions=[
            {
                "session_id": "s-1",
                "therapist_id": str(t1),
                "appointment_start": _dt(10).isoformat(),
                "appointment_end": _dt(11).isoformat(),
                "extra_minutes": 0,
                "total_session_duration": 60,
            }
        ],
    )
    svc._repo = AsyncMock()
    svc._repo.get_by_id = AsyncMock(return_value=booking)
    svc._repo.update = AsyncMock(side_effect=lambda b: b)
    svc._repo.check_therapist_overlap = AsyncMock(return_value=False)
    svc._enqueue_event = AsyncMock()

    updated = await svc.update_booking(
        booking_id=booking.id,
        therapist_id=t2,
        therapist_data={"id": str(t2), "name": "Therapist B"},
        appointment_start=_dt(14),
        extra_minutes=30,
        total_duration=90,
    )

    assert str(updated.therapist_id) == str(t2)
    assert updated.extra_minutes == 30
    assert updated.sessions[0]["therapist_id"] == str(t2)
    assert updated.sessions[0]["extra_minutes"] == 30
    assert updated.sessions[0]["total_session_duration"] == 90
    assert updated.sessions[0]["appointment_start"] == _dt(14).isoformat()
    assert updated.sessions[0]["appointment_end"] == _dt(15, 30).isoformat()


@pytest.mark.asyncio
async def test_update_booking_syncs_multi_sessions():
    import uuid
    svc = BookingService(session=AsyncMock(), settings=MagicMock())
    t1 = uuid.UUID(A)
    t2 = uuid.UUID(B)
    booking = _make_booking(
        therapist_id=t1,
        therapist_data={"id": str(t1), "name": "Therapist A"},
        appointment_start=_dt(10),
        appointment_end=_dt(12),
        duration_minutes=120,
        total_duration=120,
        sessions=[
            {
                "session_id": "s-1",
                "therapist_id": str(t1),
                "appointment_start": _dt(10).isoformat(),
                "appointment_end": _dt(11).isoformat(),
                "extra_minutes": 0,
                "total_session_duration": 60,
            },
            {
                "session_id": "s-2",
                "therapist_id": str(t2),
                "appointment_start": _dt(11).isoformat(),
                "appointment_end": _dt(12).isoformat(),
                "extra_minutes": 0,
                "total_session_duration": 60,
            },
        ],
    )
    svc._repo = AsyncMock()
    svc._repo.get_by_id = AsyncMock(return_value=booking)
    svc._repo.update = AsyncMock(side_effect=lambda b: b)
    svc._repo.check_therapist_overlap = AsyncMock(return_value=False)
    svc._enqueue_event = AsyncMock()

    # Shift booking by 2 hours (10:00 -> 12:00) and add 30 extra minutes
    updated = await svc.update_booking(
        booking_id=booking.id,
        appointment_start=_dt(12),
        extra_minutes=30,
        total_duration=150,
    )

    assert updated.sessions[0]["appointment_start"] == _dt(12).isoformat()
    assert updated.sessions[0]["appointment_end"] == _dt(13).isoformat()
    assert updated.sessions[1]["appointment_start"] == _dt(13).isoformat()
    assert updated.sessions[1]["extra_minutes"] == 30
    assert updated.sessions[1]["total_session_duration"] == 90
    assert updated.sessions[1]["appointment_end"] == _dt(14, 30).isoformat()


