"""
app/booking/domain/rules.py
────────────────────────────
Booking domain business rules.

All functions are pure (no I/O, no external dependencies).
These are the canonical source of truth for domain constraints.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.booking.domain.value_objects import BookingStatus, ServiceType
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidRescheduleError,
    RescheduleWindowError,
)


def assert_reschedule_window(
    appointment_start: datetime,
    *,
    now: datetime | None = None,
    min_hours_before: int = 6,
) -> None:
    """
    Validate that a reschedule request is made sufficiently in advance.

    The appointment must be at least *min_hours_before* hours in the future.

    Args:
        appointment_start: Timezone-aware datetime of the appointment start.
        now: Current time (injected for testability). Defaults to utcnow().
        min_hours_before: Minimum hours of notice required.

    Raises:
        RescheduleWindowError: if the appointment is too close.
    """
    if appointment_start.tzinfo is None:
        raise ValueError("appointment_start must be timezone-aware.")

    _now = now or datetime.now(tz=ZoneInfo("UTC"))
    cutoff = _now + timedelta(hours=min_hours_before)

    if appointment_start <= cutoff:
        raise RescheduleWindowError(
            f"Rescheduling must be requested at least {min_hours_before} hours "
            f"before the appointment. "
            f"Appointment starts at {appointment_start.isoformat()}, "
            f"current time is {_now.isoformat()}.",
            code="RESCHEDULE_WINDOW_VIOLATION",
        )


def assert_booking_is_reschedulable(current_status: BookingStatus) -> None:
    """
    Only CONFIRMED bookings can request rescheduling.

    Raises:
        InvalidRescheduleError: if the booking is not in a reschedulable state.
    """
    if current_status != BookingStatus.CONFIRMED:
        raise InvalidRescheduleError(
            f"Only confirmed bookings can request rescheduling. "
            f"Current status: '{current_status.value}'.",
            code="BOOKING_NOT_RESCHEDULABLE",
        )


def assert_home_service_eligible(is_available_for_home_service: bool) -> None:
    """
    Raise if a therapist is not eligible for home service.

    Args:
        is_available_for_home_service: The therapist's home-service flag.

    Raises:
        ValidationError: if the therapist is not eligible.
    """
    from app.core.exceptions import ValidationError

    if not is_available_for_home_service:
        raise ValidationError(
            "The selected therapist is not eligible for home service bookings.",
            code="THERAPIST_NOT_HOME_SERVICE_ELIGIBLE",
        )


def assert_service_duration_fits(
    slot_start: datetime,
    slot_end: datetime,
    duration_minutes: int,
) -> None:
    """
    Validate that a time slot can accommodate the required service duration.

    Args:
        slot_start: Slot start time (timezone-aware).
        slot_end: Slot end time (timezone-aware).
        duration_minutes: Required service duration.

    Raises:
        ValidationError: if the slot is too short.
    """
    from app.core.exceptions import ValidationError

    available_minutes = int((slot_end - slot_start).total_seconds() / 60)
    if available_minutes < duration_minutes:
        raise ValidationError(
            f"The selected time slot ({available_minutes} min) is shorter than "
            f"the service duration ({duration_minutes} min).",
            code="SLOT_TOO_SHORT",
        )
