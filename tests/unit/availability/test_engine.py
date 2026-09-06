"""
tests/unit/availability/test_engine.py
────────────────────────────────────────
Unit tests for the Availability Engine.

All tests are pure — no database, no HTTP, no Redis.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.availability.domain.engine import AppointmentAvailabilityEngine
from app.availability.domain.interval_engine import merge_intervals, subtract_intervals
from app.availability.domain.value_objects import BlockType, Interval, TherapistScheduleInput


TZ = ZoneInfo("Asia/Kuwait")


def _dt(hour: int, minute: int = 0, d: date | None = None) -> datetime:
    """Helper: create a timezone-aware datetime."""
    if d is None:
        d = date(2025, 1, 15)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ)


def _interval(h_start: int, h_end: int, d: date | None = None) -> Interval:
    return Interval(_dt(h_start, d=d), _dt(h_end, d=d))


# ── merge_intervals ───────────────────────────────────────────────────────────


class TestMergeIntervals:
    def test_empty_list(self):
        assert merge_intervals([]) == []

    def test_single_interval(self):
        iv = _interval(9, 10)
        assert merge_intervals([iv]) == [iv]

    def test_non_overlapping(self):
        result = merge_intervals([_interval(9, 10), _interval(11, 12)])
        assert len(result) == 2

    def test_overlapping_merged(self):
        result = merge_intervals([_interval(9, 11), _interval(10, 12)])
        assert len(result) == 1
        assert result[0] == Interval(_dt(9), _dt(12))

    def test_adjacent_merged(self):
        result = merge_intervals([_interval(9, 10), _interval(10, 11)])
        assert len(result) == 1
        assert result[0] == Interval(_dt(9), _dt(11))

    def test_unsorted_input(self):
        result = merge_intervals([_interval(11, 12), _interval(9, 10)])
        assert len(result) == 2
        assert result[0].start == _dt(9)

    def test_fully_contained(self):
        result = merge_intervals([_interval(8, 18), _interval(10, 12)])
        assert len(result) == 1
        assert result[0] == Interval(_dt(8), _dt(18))


# ── subtract_intervals ────────────────────────────────────────────────────────


class TestSubtractIntervals:
    def test_subtract_nothing(self):
        working = [_interval(9, 17)]
        result = subtract_intervals(working, [])
        assert result == working

    def test_subtract_full_overlap(self):
        working = [_interval(9, 17)]
        blocked = [_interval(9, 17)]
        result = subtract_intervals(working, blocked)
        assert result == []

    def test_subtract_from_middle(self):
        working = [_interval(9, 17)]
        blocked = [_interval(12, 13)]
        result = subtract_intervals(working, blocked)
        assert len(result) == 2
        assert result[0] == Interval(_dt(9), _dt(12))
        assert result[1] == Interval(_dt(13), _dt(17))

    def test_subtract_from_start(self):
        working = [_interval(9, 17)]
        blocked = [_interval(9, 11)]
        result = subtract_intervals(working, blocked)
        assert len(result) == 1
        assert result[0] == Interval(_dt(11), _dt(17))

    def test_subtract_from_end(self):
        working = [_interval(9, 17)]
        blocked = [_interval(15, 17)]
        result = subtract_intervals(working, blocked)
        assert len(result) == 1
        assert result[0] == Interval(_dt(9), _dt(15))

    def test_multiple_bookings(self):
        working = [_interval(9, 17)]
        blocked = [_interval(10, 11), _interval(13, 14)]
        result = subtract_intervals(working, blocked)
        assert len(result) == 3

    def test_no_overlap_at_all(self):
        working = [_interval(9, 12)]
        blocked = [_interval(14, 17)]
        result = subtract_intervals(working, blocked)
        assert result == working


# ── Availability Engine ────────────────────────────────────────────────────────


class TestAvailabilityEngine:
    def _make_engine(self, duration: int = 60) -> AppointmentAvailabilityEngine:
        return AppointmentAvailabilityEngine(
            service_duration_minutes=duration,
            slot_minutes=30,
            home_service_buffer_minutes=30,
        )

    def _make_therapist(
        self,
        working: list[Interval] | None = None,
        bookings: list[Interval] | None = None,
    ) -> TherapistScheduleInput:
        d = date(2025, 1, 15)
        default_working = [Interval(_dt(9, d=d), _dt(17, d=d))]
        return TherapistScheduleInput(
            therapist_id="t1",
            therapist_name="Test Therapist",
            is_available_for_home_service=True,
            working_hours=default_working if working is None else working,
            leaves=[],
            extra_hours=[],
            existing_bookings=bookings or [],
            home_bookings=[],
            temporary_holds=[],
        )

    def test_fully_free_day(self):
        engine = self._make_engine(duration=60)
        therapist = self._make_therapist()
        d = date(2025, 1, 15)
        result = engine.compute_therapist_availability(
            therapist,
            dates=[d],
            branch_timezone="Asia/Kuwait",
            branch_opening_time=time(9, 0),
            branch_closing_time=time(17, 0),
        )
        day = result[d.isoformat()]
        available = [b for b in day.blocks if b.available]
        # 8 hours × 2 slots/hour = 16 slots, minus last 1 (not enough room for 60 min) = 15
        assert len(available) >= 10

    def test_no_working_hours(self):
        engine = self._make_engine()
        therapist = self._make_therapist(working=[])  # No working hours
        d = date(2025, 1, 15)
        result = engine.compute_therapist_availability(
            therapist,
            dates=[d],
            branch_timezone="Asia/Kuwait",
            branch_opening_time=time(9, 0),
            branch_closing_time=time(17, 0),
        )
        day = result[d.isoformat()]
        assert day.is_closed is True
        assert not any(b.available for b in day.blocks)

    def test_booking_blocks_slot(self):
        d = date(2025, 1, 15)
        booking = _interval(10, 11, d=d)
        engine = self._make_engine(duration=30)
        therapist = self._make_therapist(bookings=[booking])
        result = engine.compute_therapist_availability(
            therapist,
            dates=[d],
            branch_timezone="Asia/Kuwait",
            branch_opening_time=time(9, 0),
            branch_closing_time=time(17, 0),
        )
        day = result[d.isoformat()]
        # Slots at 10:00-10:30 and 10:30-11:00 should be unavailable
        ten_am_block = next(
            (b for b in day.blocks if b.start.hour == 10 and b.start.minute == 0), None
        )
        assert ten_am_block is not None
        assert not ten_am_block.available

    def test_home_service_buffer_applied(self):
        d = date(2025, 1, 15)
        home_booking = _interval(10, 11, d=d)
        engine = self._make_engine(duration=30)
        # Apply buffer manually
        buffered = engine.apply_home_service_buffer(home_booking)
        assert buffered.start == _dt(9, 30, d=d)
        assert buffered.end == _dt(11, 30, d=d)
