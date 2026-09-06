"""
app/availability/domain/value_objects.py
──────────────────────────────────────────
Immutable value objects for the Availability engine.

Design:
- All intervals are represented as (start, end) datetime pairs.
- Interval arithmetic is O(n log n) via merge-sort based merging.
- BlockType is a stable enum — never use raw strings in the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import NamedTuple


class BlockType(str, Enum):
    """
    Reason a time block is unavailable.

    Used by the frontend to render the calendar with appropriate indicators.
    """

    AVAILABLE = "available"
    BOOKING = "booking"
    HOME_BOOKING = "home_booking"
    LEAVE = "leave"
    OUTSIDE_WORKING_HOURS = "outside_working_hours"
    OUTSIDE_BRANCH_HOURS = "outside_branch_hours"
    TEMPORARY_HOLD = "temporary_hold"


class Interval(NamedTuple):
    """A half-open time interval [start, end)."""

    start: datetime
    end: datetime

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and self.end > other.start

    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def __repr__(self) -> str:
        return f"Interval({self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')})"


@dataclass(frozen=True)
class TimeBlock:
    """A 30-minute calendar slot with availability status."""

    start: datetime
    end: datetime
    available: bool
    blocking_type: BlockType = BlockType.AVAILABLE
    booking_id: str | None = None     # ID of the blocking booking (if any)
    resource_type: str | None = None  # e.g. "therapist", "arrangement"

    def to_display(self) -> dict[str, object]:
        """Serialise to the API response format."""
        result: dict[str, object] = {
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
            "available": self.available,
        }
        if not self.available:
            result["blocking_type"] = self.blocking_type.value
        if self.booking_id:
            result["booking_id"] = self.booking_id
        return result


@dataclass(frozen=True)
class DayAvailability:
    """Computed availability for a single calendar date."""

    date_str: str          # YYYY-MM-DD
    branch_opening: str    # HH:MM
    branch_closing: str    # HH:MM
    blocks: list[TimeBlock] = field(default_factory=list)
    is_closed: bool = False


@dataclass(frozen=True)
class TherapistScheduleInput:
    """Raw schedule data for one therapist, used as engine input."""

    therapist_id: str
    therapist_name: str
    is_available_for_home_service: bool
    working_hours: list[Interval]      # Regular weekly working intervals for the date range
    leaves: list[Interval]             # Leave intervals
    extra_hours: list[Interval]        # Extra working intervals
    existing_bookings: list[Interval]  # Confirmed/pending booking intervals
    home_bookings: list[Interval]      # Home service booking intervals (with buffer applied)
    temporary_holds: list[Interval]    # Temporary payment-hold intervals
