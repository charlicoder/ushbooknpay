"""
app/api/v1/availability.py
───────────────────────────
Availability API endpoints.

Routes:
  GET /api/v1/availability/                         → Query availability for a service
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings, DBSession, RedisClient, RequireAppToken, USHAuthDep
from app.availability.domain.engine import AppointmentAvailabilityEngine
from app.availability.domain.interval_engine import merge_intervals
from app.availability.domain.value_objects import Interval, TherapistScheduleInput
from app.booking.infrastructure.repository import BookingRepository
from app.common.utils import date_range, local_today
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/availability", tags=["Availability"])


@router.get(
    "/",
    summary="Query therapist availability for a service",
    description=(
        "Returns available 30-minute time slots for a service across all qualified therapists "
        "at a branch for the next N days. Accounts for existing bookings, leaves, working hours, "
        "and temporary holds. Requires USHSPA-TOKEN header (public for mobile client/guests)."
    ),
)
async def get_availability(
    _: RequireAppToken,
    session: DBSession,
    ushauth: USHAuthDep,
    settings: AppSettings,
    branch_id: uuid.UUID = Query(..., description="Branch ID"),
    service_id: uuid.UUID = Query(..., description="Service ID"),
    therapist_id: uuid.UUID | None = Query(
        default=None, description="Specific therapist (optional)"
    ),
    days_ahead: int = Query(
        default=10, ge=1, le=60,
        description="Number of days to compute availability for"
    ),
    service_type: str = Query(default="branch", description="branch or home"),
) -> JSONResponse:
    """
    Compute availability for the given service/branch combination.

    Algorithm:
    1. Fetch all therapists for the service from ushauth (cached).
    2. Fetch their schedules and existing bookings in a single batch query.
    3. Run the O(n log n) interval engine per therapist per day.
    4. Return structured time blocks.
    """
    # ── Fetch catalog data ──────────────────────────────────────────────
    branch_data = await ushauth.get_branch(str(branch_id))
    service_data = await ushauth.get_service(str(service_id))

    branch_timezone = branch_data.get("timezone", "Asia/Kuwait")
    opening_time_str: str | None = branch_data.get("opening_time")
    closing_time_str: str | None = branch_data.get("closing_time")

    if not opening_time_str or not closing_time_str:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Branch does not have configured opening/closing times.",
        )

    from datetime import time as dt_time

    def _parse_time(t: str) -> dt_time:
        parts = t.split(":")
        return dt_time(int(parts[0]), int(parts[1]))

    branch_opening = _parse_time(opening_time_str)
    branch_closing = _parse_time(closing_time_str)
    duration_minutes: int = service_data.get("duration_minutes", 60)

    # ── Determine dates ─────────────────────────────────────────────────
    today = local_today(branch_timezone)
    dates = date_range(today, days_ahead)

    # ── Fetch therapists ────────────────────────────────────────────────
    if therapist_id:
        raw_therapists = [await ushauth.get_therapist(str(therapist_id))]
    else:
        raw_therapists = await ushauth.list_therapists_for_service(str(service_id))

    # Filter for home service eligibility
    if service_type == "home":
        raw_therapists = [t for t in raw_therapists if t.get("can_do_home_service")]

    if not raw_therapists:
        return JSONResponse(
            content={
                "success": True,
                "data": [],
                "meta": {
                    "branch_id": str(branch_id),
                    "service_id": str(service_id),
                    "days_ahead": days_ahead,
                    "message": "No available therapists for this service.",
                },
            }
        )

    # ── Batch fetch existing bookings ───────────────────────────────────
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(branch_timezone)
    range_start = datetime.combine(dates[0], branch_opening, tzinfo=tz)
    range_end = datetime.combine(dates[-1], branch_closing, tzinfo=tz)

    repo = BookingRepository(session)
    therapist_uuids = [uuid.UUID(t["id"]) for t in raw_therapists]
    all_bookings = await repo.get_active_bookings_for_therapists_in_range(
        therapist_uuids, range_start, range_end
    )

    # Group bookings by therapist_id
    from collections import defaultdict
    bookings_by_therapist: dict[str, list] = defaultdict(list)
    for bk in all_bookings:
        bookings_by_therapist[str(bk.therapist_id)].append(bk)

    # Fetch temporary holds
    all_holds = []
    for t_id in therapist_uuids:
        holds = await repo.get_active_holds_for_therapist_in_range(
            t_id, range_start, range_end
        )
        for h in holds:
            bookings_by_therapist[str(t_id)]  # ensure key exists
            all_holds.append((str(t_id), h))

    # ── Run availability engine ──────────────────────────────────────────
    engine = AppointmentAvailabilityEngine(
        service_duration_minutes=duration_minutes,
        slot_minutes=settings.SLOT_DURATION_MINUTES,
        home_service_buffer_minutes=settings.HOME_SERVICE_BUFFER_MINUTES,
    )

    result_data = []
    for therapist_raw in raw_therapists:
        t_id_str = str(therapist_raw.get("id", ""))
        t_bookings = bookings_by_therapist.get(t_id_str, [])
        t_holds = [h for tid, h in all_holds if tid == t_id_str]

        # Build working hours from therapist schedule
        # NOTE: ushauth TherapistDetail has schedule in working_hours field
        schedule = therapist_raw.get("schedule", [])

        booking_intervals = [
            Interval(b.appointment_start, b.appointment_end) for b in t_bookings
        ]
        home_booking_intervals: list[Interval] = []
        hold_intervals = [
            Interval(h.appointment_start, h.appointment_end) for h in t_holds
        ]

        # Convert schedule to working intervals for the date range
        working_intervals: list[Interval] = []
        for sched_entry in schedule:
            try:
                from datetime import datetime as dt

                work_start = dt.fromisoformat(sched_entry.get("start_datetime", ""))
                work_end = dt.fromisoformat(sched_entry.get("end_datetime", ""))
                if work_start.tzinfo is None:
                    work_start = work_start.replace(tzinfo=tz)
                if work_end.tzinfo is None:
                    work_end = work_end.replace(tzinfo=tz)
                working_intervals.append(Interval(work_start, work_end))
            except Exception:
                continue

        therapist_input = TherapistScheduleInput(
            therapist_id=t_id_str,
            therapist_name=(
                f"{therapist_raw.get('first_name', '')} "
                f"{therapist_raw.get('last_name', '')}".strip()
            ),
            is_available_for_home_service=therapist_raw.get("can_do_home_service", False),
            working_hours=merge_intervals(working_intervals) if working_intervals else [],
            leaves=[],  # Would come from leave management — not in ushauth v1
            extra_hours=[],
            existing_bookings=merge_intervals(booking_intervals),
            home_bookings=merge_intervals(home_booking_intervals),
            temporary_holds=merge_intervals(hold_intervals),
        )

        day_availability = engine.compute_therapist_availability(
            therapist_input,
            dates=dates,
            branch_timezone=branch_timezone,
            branch_opening_time=branch_opening,
            branch_closing_time=branch_closing,
        )

        result_data.append(
            {
                "therapist_id": t_id_str,
                "therapist_name": therapist_input.therapist_name,
                "is_home_service_eligible": therapist_input.is_available_for_home_service,
                "availability": {
                    date_str: {
                        "date": date_str,
                        "is_closed": day.is_closed,
                        "branch_opening": day.branch_opening,
                        "branch_closing": day.branch_closing,
                        "blocks": [block.to_display() for block in day.blocks],
                    }
                    for date_str, day in day_availability.items()
                },
            }
        )

    return JSONResponse(
        content={
            "success": True,
            "data": result_data,
            "meta": {
                "branch_id": str(branch_id),
                "service_id": str(service_id),
                "service_duration_minutes": duration_minutes,
                "slot_duration_minutes": settings.SLOT_DURATION_MINUTES,
                "days_ahead": days_ahead,
                "timezone": branch_timezone,
                "therapist_count": len(result_data),
            },
        }
    )
