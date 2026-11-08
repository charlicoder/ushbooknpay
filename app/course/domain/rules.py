"""
app/course/domain/rules.py
──────────────────────────
Pure course-subscription rules (no I/O). Shared by the application service,
the booking integration and the API serializers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.course.domain.value_objects import (
    COURSE_APPOINTMENT_AFTER_EXPIRY,
    COURSE_ARRANGEMENT_MISMATCH,
    COURSE_BOOKING_TYPE,
    COURSE_CUSTOMER_MISMATCH,
    COURSE_EXTRAS_NOT_ALLOWED,
    COURSE_NO_SESSIONS_AVAILABLE,
    COURSE_PAYMENT_TYPE,
    COURSE_SERVICE_MISMATCH,
    COURSE_SINGLE_SESSION_ONLY,
    COURSE_SUBSCRIPTION_EXPIRED,
    COURSE_SUBSCRIPTION_NOT_ACTIVE,
    REFUNDABLE_STATUSES,
    CourseError,
    CourseSubscriptionStatus,
)

_KWD = Decimal("0.001")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except Exception:
        return Decimal("0")


# ── Pricing / validity ────────────────────────────────────────────────────────


def compute_course_price(course: dict[str, Any]) -> Decimal:
    """
    Server-side purchase price (contract B):
    ``price`` if > 0, else ``unit_price × total_sessions`` (unit = service base price),
    quantized to 0.001 KWD. Falls back to ushauth's ``effective_price`` only when the
    unit price is unknown. Client-supplied prices are never used.
    """
    price = _dec(course.get("price"))
    if price > 0:
        return price.quantize(_KWD, rounding=ROUND_HALF_UP)
    total = int(course.get("total_sessions") or 0)
    unit = _dec(course.get("unit_price") or (course.get("service") or {}).get("base_price"))
    if unit > 0 and total > 0:
        return (unit * total).quantize(_KWD, rounding=ROUND_HALF_UP)
    return _dec(course.get("effective_price")).quantize(_KWD, rounding=ROUND_HALF_UP)


def compute_expires_at(activated_at: datetime, validity_days: int) -> datetime:
    """Validity starts at payment activation (rule A5)."""
    return _aware(activated_at) + timedelta(days=int(validity_days))


# ── Balance / status ──────────────────────────────────────────────────────────


def available_sessions(sub: Any) -> int:
    return max(
        int(sub.total_sessions or 0) - int(sub.used_sessions or 0) - int(sub.reserved_sessions or 0),
        0,
    )


def is_expired(sub: Any, now: datetime | None = None) -> bool:
    exp = _aware(getattr(sub, "expires_at", None))
    if exp is None:
        return sub.status == CourseSubscriptionStatus.EXPIRED.value
    return (now or _utcnow()) >= exp or sub.status == CourseSubscriptionStatus.EXPIRED.value


def effective_status(sub: Any, now: datetime | None = None) -> str:
    """``expired`` when status is active and ``now >= expires_at``; otherwise status."""
    status = str(sub.status)
    if status == CourseSubscriptionStatus.ACTIVE.value:
        exp = _aware(getattr(sub, "expires_at", None))
        if exp is not None and (now or _utcnow()) >= exp:
            return CourseSubscriptionStatus.EXPIRED.value
    return status


def is_redeemable(sub: Any, now: datetime | None = None) -> bool:
    return effective_status(sub, now) == CourseSubscriptionStatus.ACTIVE.value and available_sessions(sub) > 0


def can_refund(sub: Any) -> bool:
    """Rule A5: full refund only when nothing used/booked and status active/expired."""
    return (
        str(sub.status) in REFUNDABLE_STATUSES
        and int(sub.used_sessions or 0) == 0
        and int(sub.reserved_sessions or 0) == 0
    )


# ── Booking helpers ───────────────────────────────────────────────────────────


def _real_uuid(value: Any) -> bool:
    if isinstance(value, uuid.UUID):
        return True
    if isinstance(value, str) and value.strip():
        try:
            uuid.UUID(value.strip())
            return True
        except ValueError:
            return False
    return False


def is_course_booking(booking: Any) -> bool:
    """True for course redemption bookings (safe on MagicMock / legacy rows)."""
    if booking is None:
        return False
    b_type = getattr(booking, "booking_type", None)
    p_type = getattr(booking, "payment_type", None)
    if isinstance(b_type, str) and b_type.strip().lower() == COURSE_BOOKING_TYPE:
        return True
    if isinstance(p_type, str) and p_type.strip().lower() == COURSE_PAYMENT_TYPE:
        return True
    return _real_uuid(getattr(booking, "course_subscription_id", None))


def _arrangement_type_of(entry_or_session: dict[str, Any]) -> str:
    ad = entry_or_session.get("service_arrangement_data") or {}
    return str(
        entry_or_session.get("arrangement_type") or ad.get("arrangement_type") or ad.get("type") or ""
    ).strip().lower()


def validate_reservation(
    sub: Any,
    *,
    customer_id: Any,
    services: list[dict[str, Any]],
    appointment_start: datetime,
    sessions: list[dict[str, Any]] | None = None,
    addons: list[Any] | None = None,
    extra_minutes: int = 0,
    arrangement_type: str | None = None,
    now: datetime | None = None,
) -> None:
    """
    Contract E validation, executed on the LOCKED subscription row.
    Raises :class:`CourseError` with the contract error code.
    """
    now = now or _utcnow()
    if str(sub.customer_id) != str(customer_id):
        raise CourseError(COURSE_CUSTOMER_MISMATCH, "This course subscription belongs to another customer.")

    eff = effective_status(sub, now)
    if eff == CourseSubscriptionStatus.EXPIRED.value:
        raise CourseError(COURSE_SUBSCRIPTION_EXPIRED, "This course subscription has expired.")
    if eff != CourseSubscriptionStatus.ACTIVE.value:
        raise CourseError(
            COURSE_SUBSCRIPTION_NOT_ACTIVE,
            f"This course subscription is not active (status: {eff}).",
        )
    if available_sessions(sub) <= 0:
        raise CourseError(COURSE_NO_SESSIONS_AVAILABLE, "No sessions left on this course subscription.")

    exp = _aware(sub.expires_at)
    start = _aware(appointment_start)
    if exp is not None and start is not None and start >= exp:
        raise CourseError(
            COURSE_APPOINTMENT_AFTER_EXPIRY,
            "The appointment must start before the course subscription expires.",
            detail={"expires_at": exp.isoformat()},
        )

    if sessions and len(sessions) > 1:
        raise CourseError(COURSE_SINGLE_SESSION_ONLY, "A course redemption booking can contain only one session.")

    if len(services or []) != 1 or str((services or [{}])[0].get("service_id")) != str(sub.service_id):
        raise CourseError(COURSE_SERVICE_MISMATCH, "A course redemption booking must contain exactly the course service.")

    session_addons: list[Any] = []
    session_extra = 0
    for sess in sessions or []:
        session_addons.extend(sess.get("addons") or [])
        session_extra += int(sess.get("extra_minutes") or 0)
    if (addons or session_addons) or int(extra_minutes or 0) > 0 or session_extra > 0:
        raise CourseError(COURSE_EXTRAS_NOT_ALLOWED, "Add-ons and extra minutes are not allowed on course sessions.")

    expected = str(sub.arrangement_type or "").strip().lower()
    actual = (arrangement_type or "").strip().lower()
    if not actual:
        source = (sessions or [None])[0] or (services or [{}])[0]
        actual = _arrangement_type_of(source or {})
    if expected and actual != expected:
        raise CourseError(
            COURSE_ARRANGEMENT_MISMATCH,
            "The selected room type does not match the course room type.",
            detail={"expected": expected, "actual": actual or None},
        )


def validate_booking_update(
    sub: Any,
    *,
    services: list[dict[str, Any]],
    sessions: list[dict[str, Any]] | None,
    addons: list[Any] | None,
    extra_minutes: int,
) -> None:
    """Edits of a course booking must keep it a plain single-service session."""
    if sessions and len(sessions) > 1:
        raise CourseError(COURSE_SINGLE_SESSION_ONLY, "A course redemption booking can contain only one session.")
    if len(services or []) != 1 or str((services or [{}])[0].get("service_id")) != str(sub.service_id):
        raise CourseError(COURSE_SERVICE_MISMATCH, "A course redemption booking must contain exactly the course service.")
    if addons or int(extra_minutes or 0) > 0:
        raise CourseError(COURSE_EXTRAS_NOT_ALLOWED, "Add-ons and extra minutes are not allowed on course sessions.")


def assert_start_before_expiry(sub: Any, appointment_start: datetime | None) -> None:
    exp = _aware(sub.expires_at)
    start = _aware(appointment_start)
    if exp is not None and start is not None and start >= exp:
        raise CourseError(
            COURSE_APPOINTMENT_AFTER_EXPIRY,
            "The appointment must start before the course subscription expires.",
            detail={"expires_at": exp.isoformat()},
        )


def next_session_number(active_numbers: list[int]) -> int:
    """Lowest positive number not used by a non-released usage (keeps numbers ≤ total)."""
    taken = set(int(n) for n in active_numbers if n)
    n = 1
    while n in taken:
        n += 1
    return n

