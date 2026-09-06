"""
app/availability/domain/engine.py
───────────────────────────────────
AppointmentAvailabilityEngine — the top-level orchestrator.

Architecture:
    AppointmentAvailabilityEngine
        → ScheduleResolver       (fetch + normalise raw data)
        → WorkingIntervalCalculator (compute working time)
        → ConflictResolver       (aggregate all blocked intervals)
        → IntervalIntersectionEngine (subtract blocked from working)
        → BlockGenerator         (produce 30-min blocks)
        → AvailabilityResponseBuilder (assemble API response)

Algorithm Complexity:
    O(n log n) per therapist per day where n = number of intervals.
    Overall O(T × D × n log n) where T=therapists, D=days, n=intervals per therapist.

    For 100 therapists × 10 days × 50 intervals each:
    ≈ 100 × 10 × 50 × log(50) ≈ 282,000 operations — sub-millisecond.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.availability.domain.interval_engine import (
    generate_blocks,
    merge_intervals,
    subtract_intervals,
)
from app.availability.domain.value_objects import (
    BlockType,
    DayAvailability,
    Interval,
    TherapistScheduleInput,
    TimeBlock,
)
from app.common.utils import date_range
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class AppointmentAvailabilityEngine:
    """
    Computes appointment availability for therapists and service arrangements.

    Pure computation — takes pre-fetched data and returns structured availability.
    No database or cache access in this class.
    """

    def __init__(
        self,
        service_duration_minutes: int,
        slot_minutes: int = 30,
        home_service_buffer_minutes: int = 30,
    ) -> None:
        self._duration = service_duration_minutes
        self._slot = slot_minutes
        self._home_buffer = home_service_buffer_minutes

    def compute_therapist_availability(
        self,
        therapist: TherapistScheduleInput,
        *,
        dates: list[date],
        branch_timezone: str,
        branch_opening_time: time,
        branch_closing_time: time,
    ) -> dict[str, DayAvailability]:
        """
        Compute daily availability for a single therapist.

        Returns a mapping of ISO date string → DayAvailability.
        """
        tz = ZoneInfo(branch_timezone)
        result: dict[str, DayAvailability] = {}

        for d in dates:
            date_str = d.isoformat()

            # Build branch open/close datetimes for this day
            branch_open = datetime.combine(d, branch_opening_time, tzinfo=tz)
            branch_close = datetime.combine(d, branch_closing_time, tzinfo=tz)

            # Day working window
            day_window = Interval(branch_open, branch_close)

            # ── Step 1: Filter intervals relevant to this day ─────────
            def _filter_for_day(ivs: list[Interval]) -> list[Interval]:
                return [iv for iv in ivs if iv.overlaps(day_window)]

            working_for_day = _filter_for_day(therapist.working_hours)
            leaves_for_day = _filter_for_day(therapist.leaves)
            extra_for_day = _filter_for_day(therapist.extra_hours)
            bookings_for_day = _filter_for_day(therapist.existing_bookings)
            home_bookings_for_day = _filter_for_day(therapist.home_bookings)
            holds_for_day = _filter_for_day(therapist.temporary_holds)

            # ── Step 2: Compute effective working intervals ────────────
            # Base working = regular hours + extra hours
            # Then clamp to branch open/close
            base_working = merge_intervals(working_for_day + extra_for_day)
            # Clamp to branch hours
            branch_interval = [day_window]
            effective_working = _intersect_with_window(base_working, day_window)

            # ── Step 3: Remove leaves ─────────────────────────────────
            effective_working = subtract_intervals(
                effective_working, merge_intervals(leaves_for_day)
            )

            # ── Step 4: Aggregate all blocking intervals ───────────────
            # Home bookings include the buffer (caller should pre-apply)
            all_blocked = merge_intervals(
                bookings_for_day + home_bookings_for_day + holds_for_day
            )

            # ── Step 5: Subtract blocked from working ─────────────────
            free_intervals = subtract_intervals(effective_working, all_blocked)

            # ── Step 6: Generate 30-min blocks ────────────────────────
            if not effective_working:
                # Therapist has no working time today → all blocks unavailable
                blocks = _generate_closed_blocks(
                    branch_open, branch_close, self._slot
                )
            else:
                blocks = generate_blocks(
                    free_intervals=free_intervals,
                    all_intervals=effective_working,
                    blocked_intervals=all_blocked,
                    slot_minutes=self._slot,
                    branch_open=branch_open,
                    branch_close=branch_close,
                    service_duration_minutes=self._duration,
                )

            result[date_str] = DayAvailability(
                date_str=date_str,
                branch_opening=branch_opening_time.strftime("%H:%M"),
                branch_closing=branch_closing_time.strftime("%H:%M"),
                blocks=blocks,
                is_closed=not bool(effective_working),
            )

        return result

    def apply_home_service_buffer(self, interval: Interval) -> Interval:
        """
        Expand a home-service booking interval by the configured buffer.

        buffer_before + service_duration + buffer_after
        """
        buf = timedelta(minutes=self._home_buffer)
        return Interval(interval.start - buf, interval.end + buf)


def _intersect_with_window(
    intervals: list[Interval],
    window: Interval,
) -> list[Interval]:
    """Clip each interval to the given window."""
    result: list[Interval] = []
    for iv in intervals:
        start = max(iv.start, window.start)
        end = min(iv.end, window.end)
        if start < end:
            result.append(Interval(start, end))
    return result


def _generate_closed_blocks(
    branch_open: datetime,
    branch_close: datetime,
    slot_minutes: int,
) -> list[TimeBlock]:
    """Return all-unavailable blocks for a closed day."""
    blocks: list[TimeBlock] = []
    current = branch_open
    slot = timedelta(minutes=slot_minutes)
    while current + slot <= branch_close:
        blocks.append(
            TimeBlock(
                start=current,
                end=current + slot,
                available=False,
                blocking_type=BlockType.OUTSIDE_WORKING_HOURS,
            )
        )
        current += slot
    return blocks
