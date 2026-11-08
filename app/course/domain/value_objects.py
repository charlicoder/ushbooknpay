"""
app/course/domain/value_objects.py
──────────────────────────────────
Enums and error type of the course-subscription domain (no I/O).
"""

from __future__ import annotations

from enum import Enum
from http import HTTPStatus
from typing import Any

from app.core.exceptions import USHBaseError


class CourseSubscriptionStatus(str, Enum):
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_FAILED = "payment_failed"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class CourseUsageStatus(str, Enum):
    RESERVED = "reserved"
    CONSUMED = "consumed"  # terminal
    RELEASED = "released"


class CourseInvoiceStatus(str, Enum):
    PENDING = "pending"
    CREATED = "created"
    FAILED = "failed"


class CoursePaymentOption(str, Enum):
    PAY_AT_DESK = "pay_at_desk"
    PAYMENT_LINK = "payment_link"


#: statuses from which a payment can activate the subscription
PAYABLE_STATUSES = frozenset(
    {CourseSubscriptionStatus.PAYMENT_PENDING.value, CourseSubscriptionStatus.PAYMENT_FAILED.value}
)
#: statuses reached after a successful payment
PAID_STATUSES = frozenset(
    {
        CourseSubscriptionStatus.ACTIVE.value,
        CourseSubscriptionStatus.EXHAUSTED.value,
        CourseSubscriptionStatus.EXPIRED.value,
    }
)
#: statuses where a refund is allowed (rule A5)
REFUNDABLE_STATUSES = frozenset(
    {CourseSubscriptionStatus.ACTIVE.value, CourseSubscriptionStatus.EXPIRED.value}
)

ALL_STATUSES = tuple(s.value for s in CourseSubscriptionStatus)
ALL_USAGE_STATUSES = tuple(s.value for s in CourseUsageStatus)
ALL_INVOICE_STATUSES = tuple(s.value for s in CourseInvoiceStatus)

# Booking-side markers (must match BookingType / PaymentType values)
COURSE_BOOKING_TYPE = "course_redemption"
COURSE_PAYMENT_TYPE = "course_subscription"
COURSE_PAYMENT_METHOD = "course_subscription"
COURSE_PAYMENT_PROVIDER = "Course Subscription"
COURSE_SOURCE_DOCUMENT_TYPE = "course_subscription"

# Error codes (contract D / E)
COURSE_NOT_FOUND = "COURSE_NOT_FOUND"
COURSE_NOT_AVAILABLE = "COURSE_NOT_AVAILABLE"
COURSE_SUBSCRIPTION_NOT_FOUND = "COURSE_SUBSCRIPTION_NOT_FOUND"
COURSE_SUBSCRIPTION_NOT_ACTIVE = "COURSE_SUBSCRIPTION_NOT_ACTIVE"
COURSE_SUBSCRIPTION_EXPIRED = "COURSE_SUBSCRIPTION_EXPIRED"
COURSE_NO_SESSIONS_AVAILABLE = "COURSE_NO_SESSIONS_AVAILABLE"
COURSE_CUSTOMER_MISMATCH = "COURSE_CUSTOMER_MISMATCH"
COURSE_SERVICE_MISMATCH = "COURSE_SERVICE_MISMATCH"
COURSE_ARRANGEMENT_MISMATCH = "COURSE_ARRANGEMENT_MISMATCH"
COURSE_EXTRAS_NOT_ALLOWED = "COURSE_EXTRAS_NOT_ALLOWED"
COURSE_APPOINTMENT_AFTER_EXPIRY = "COURSE_APPOINTMENT_AFTER_EXPIRY"
COURSE_SINGLE_SESSION_ONLY = "COURSE_SINGLE_SESSION_ONLY"
COURSE_REFUND_NOT_ALLOWED = "COURSE_REFUND_NOT_ALLOWED"
COURSE_CANCEL_NOT_ALLOWED = "COURSE_CANCEL_NOT_ALLOWED"
COURSE_SUBSCRIPTION_NOT_PAYABLE = "COURSE_SUBSCRIPTION_NOT_PAYABLE"
COURSE_PAYMENT_LINK_NOT_ALLOWED = "COURSE_PAYMENT_LINK_NOT_ALLOWED"

_CONFLICT_CODES = frozenset(
    {
        COURSE_NO_SESSIONS_AVAILABLE,
        COURSE_SUBSCRIPTION_NOT_ACTIVE,
        COURSE_SUBSCRIPTION_EXPIRED,
        COURSE_REFUND_NOT_ALLOWED,
        COURSE_CANCEL_NOT_ALLOWED,
        COURSE_SUBSCRIPTION_NOT_PAYABLE,
    }
)
_NOT_FOUND_CODES = frozenset({COURSE_SUBSCRIPTION_NOT_FOUND, COURSE_NOT_FOUND})


class CourseError(USHBaseError, ValueError):
    """
    Business-rule violation of the course domain.

    Subclasses ``ValueError`` so the payment-link settlement treats a
    non-activatable subscription like a non-activatable booking (auto refund).
    HTTP status: 404 for not-found codes, 409 for state conflicts, else 400.
    """

    def __init__(self, code: str, message: str | None = None, *, detail: Any = None) -> None:
        super().__init__(message or code.replace("_", " ").capitalize(), detail=detail, code=code)
        if code in _NOT_FOUND_CODES:
            self.status_code = HTTPStatus.NOT_FOUND
        elif code in _CONFLICT_CODES:
            self.status_code = HTTPStatus.CONFLICT
        else:
            self.status_code = HTTPStatus.BAD_REQUEST

