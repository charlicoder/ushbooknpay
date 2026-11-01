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

