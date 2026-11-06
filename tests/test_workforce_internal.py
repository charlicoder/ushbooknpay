"""Pure tests for the workforce completed-sessions extraction."""
import uuid
from datetime import date
from types import SimpleNamespace

from app.api.v1.workforce_internal import extract_therapist_sessions

T1, T2 = str(uuid.uuid4()), str(uuid.uuid4())


def _booking(**kw):
    base = dict(
        id=uuid.uuid4(), booking_number="BK-1", branch_id=uuid.uuid4(), customer_id=uuid.uuid4(),
        booking_type="branch", therapist_id=uuid.UUID(T1), sessions=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_multi_session_booking_with_secondary_therapist():
    sessions = [
        {
            "session_id": str(uuid.uuid4()), "therapist_id": T1, "secondary_therapist_id": T2,
            "appointment_start": "2026-11-05T10:00:00+03:00", "appointment_end": "2026-11-05T11:15:00+03:00",
            "services": [{"service_id": "s1", "name": "Massage", "duration_minutes": 60}],
            "extra_minutes": 15, "addons_duration": 0,
        },
        {
            "session_id": str(uuid.uuid4()), "therapist_id": T1,
            "appointment_start": "2026-11-05T12:00:00+03:00", "appointment_end": "2026-11-05T12:30:00+03:00",
            "services": [{"service_data": {"name": "Scrub"}, "duration_minutes": 30}],
        },
    ]
    rows = extract_therapist_sessions(_booking(sessions=sessions), date(2026, 11, 5))
    assert [(r["therapist_id"], r["role"]) for r in rows] == [(T1, "primary"), (T2, "secondary"), (T1, "primary")]
    assert rows[0]["extra_minutes"] == 15 and rows[0]["services"][0]["name"] == "Massage"
    assert rows[2]["services"][0]["name"] == "Scrub"


def test_session_on_other_local_date_is_skipped():
    s = [{
        "session_id": "x", "therapist_id": T1,
        "appointment_start": "2026-11-05T21:30:00+00:00",  # = 2026-11-06 00:30 Kuwait
        "appointment_end": "2026-11-05T22:30:00+00:00", "services": [],
    }]
    assert extract_therapist_sessions(_booking(sessions=s), date(2026, 11, 5)) == []
    assert len(extract_therapist_sessions(_booking(sessions=s), date(2026, 11, 6))) == 1
