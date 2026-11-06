"""
app/api/v1/workforce_internal.py
─────────────────────────────────
Internal (service-to-service) read endpoint for the ushanr workforce daily job.

``GET /api/v1/internal/workforce/completed-sessions/?date=YYYY-MM-DD``
returns every session of a **completed** booking that took place on that Asia/Kuwait local
date, one entry per serving therapist (primary + optional secondary).  Cancelled, no-show,
confirmed and every other status are intentionally excluded — only completed bookings count
as actual work.  Read-only: it adds no work to any booking write path.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import DBSession, RequireAppToken
from app.booking.domain.sessions import get_booking_sessions, session_window
from app.booking.infrastructure.models import Booking
from app.common.utils import DEFAULT_TIMEZONE, to_local_tz

router = APIRouter(prefix="/internal/workforce", tags=["Internal"])

COMPLETED = "completed"


def _service_items(session: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in session.get("services") or []:
        data = e.get("service_data") or {}
        out.append(
            {
                "service_id": e.get("service_id"),
                "name": e.get("name") or e.get("service_name") or data.get("name") or data.get("service_name"),
                "duration_minutes": int(e.get("duration_minutes") or 0),
            }
        )
    return out


def extract_therapist_sessions(booking: Any, day: date) -> list[dict[str, Any]]:
    """Flatten a booking into per-therapist session dicts that start on ``day`` (local)."""
    rows: list[dict[str, Any]] = []
    for s in get_booking_sessions(booking):
        start, end = session_window(s)
        if not start or not end or to_local_tz(start).date() != day:
            continue
        base = {
            "session_id": s.get("session_id") or str(booking.id),
            "booking_id": str(booking.id),
            "booking_number": getattr(booking, "booking_number", None),
            "branch_id": str(booking.branch_id) if getattr(booking, "branch_id", None) else None,
            "customer_id": str(booking.customer_id) if getattr(booking, "customer_id", None) else None,
            "booking_type": getattr(booking, "booking_type", None),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "services": _service_items(s),
            "addons_minutes": int(s.get("addons_duration") or 0),
            "extra_minutes": int(s.get("extra_minutes") or 0),
        }
        primary = s.get("therapist_id") or (str(booking.therapist_id) if booking.therapist_id else None)
        if primary:
            rows.append({**base, "therapist_id": str(primary), "role": "primary"})
        secondary = s.get("secondary_therapist_id")
        if secondary and str(secondary) != str(primary):
            rows.append({**base, "therapist_id": str(secondary), "role": "secondary"})
    return rows


@router.get(
    "/completed-sessions/",
    summary="Completed booking sessions for a local date (internal)",
    include_in_schema=False,
)
async def completed_sessions(
    _: RequireAppToken,
    session: DBSession,
    day: str = Query(..., alias="date", description="YYYY-MM-DD (Asia/Kuwait)"),
) -> dict[str, Any]:
    try:
        d = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "`date` must be YYYY-MM-DD") from exc

    lo = datetime.combine(d, time(0, 0), tzinfo=DEFAULT_TIMEZONE)
    hi = lo + timedelta(days=1)
    bookings = (
        await session.execute(
            select(Booking).where(
                Booking.status == COMPLETED,
                Booking.appointment_date >= lo,
                Booking.appointment_date < hi,
            )
        )
    ).scalars().all()

    sessions: list[dict[str, Any]] = []
    for b in bookings:
        sessions.extend(extract_therapist_sessions(b, d))
    return {"success": True, "date": d.isoformat(), "sessions": sessions}
