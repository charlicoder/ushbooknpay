"""
app/core/exceptions.py
──────────────────────
Domain-centric exception hierarchy.

All exceptions are intentionally simple data containers.
FastAPI exception handlers in main.py translate them to HTTP responses,
keeping the domain layer completely free of HTTP concerns.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


class USHBaseError(Exception):
    """Root exception for all ushbooknpay errors."""

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: Any = None,
        code: str | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.detail = detail
        self.code = code or self.__class__.__name__
        super().__init__(self.message)


# ── HTTP-level ───────────────────────────────────────────────────────────────


class NotFoundError(USHBaseError):
    status_code = HTTPStatus.NOT_FOUND
    default_message = "Resource not found."


class ConflictError(USHBaseError):
    status_code = HTTPStatus.CONFLICT
    default_message = "A conflicting resource already exists."


class ValidationError(USHBaseError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "Validation failed."


class AuthenticationError(USHBaseError):
    status_code = HTTPStatus.UNAUTHORIZED
    default_message = "Authentication required."


class AuthorizationError(USHBaseError):
    status_code = HTTPStatus.FORBIDDEN
    default_message = "Access denied."


class RateLimitError(USHBaseError):
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    default_message = "Rate limit exceeded. Please try again later."


class IdempotencyConflictError(USHBaseError):
    """Returned when the same idempotency key has a different payload."""

    status_code = HTTPStatus.CONFLICT
    default_message = "Idempotency key already used with a different request payload."


# ── Booking Domain ───────────────────────────────────────────────────────────


class BookingNotFoundError(NotFoundError):
    default_message = "Booking not found."


class BookingStateError(USHBaseError):
    """Raised when an invalid state transition is attempted."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "Invalid booking state transition."


class DoubleBookingError(ConflictError):
    """Raised when the requested slot is no longer available."""

    default_message = (
        "The requested time slot is no longer available. Please choose another slot."
    )


class ServiceNotAvailableError(ValidationError):
    default_message = "The requested service is not available at this branch."


class TherapistNotAvailableError(ValidationError):
    default_message = "The selected therapist is not available for this slot."


class InvalidRescheduleError(ValidationError):
    default_message = "Rescheduling is not allowed for this booking."


class RescheduleWindowError(ValidationError):
    """Raised when the appointment is too close for rescheduling."""

    default_message = (
        "Rescheduling must be requested at least 6 hours before the appointment."
    )


# ── Payment Domain ───────────────────────────────────────────────────────────


class PaymentNotFoundError(NotFoundError):
    default_message = "Payment record not found."


class PaymentStateError(USHBaseError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "Invalid payment state transition."


class PaymentProviderError(USHBaseError):
    """Raised when a payment provider returns an error."""

    status_code = HTTPStatus.BAD_GATEWAY
    default_message = "Payment provider returned an error."


class RefundError(USHBaseError):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "Refund could not be processed."


# ── Integration ──────────────────────────────────────────────────────────────


class GatewayError(USHBaseError):
    """Raised when an inter-service API call fails."""

    status_code = HTTPStatus.BAD_GATEWAY
    default_message = "Upstream service call failed."


class GatewayTimeoutError(GatewayError):
    status_code = HTTPStatus.GATEWAY_TIMEOUT
    default_message = "Upstream service did not respond in time."


# ── Infrastructure ───────────────────────────────────────────────────────────


class CacheError(USHBaseError):
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "Cache operation failed."


class OutboxPublishError(USHBaseError):
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "Failed to publish event to SQS."


# ── Gift Voucher Domain ───────────────────────────────────────────────────────


class GiftVoucherNotFoundError(NotFoundError):
    default_message = "Gift voucher not found."


class GiftVoucherStateError(USHBaseError):
    """Raised when an invalid voucher status transition is attempted."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "Invalid gift voucher state transition."


class GiftVoucherExpiredError(ValidationError):
    default_message = "This gift voucher has expired."


class GiftVoucherAlreadyRedeemedError(ConflictError):
    default_message = "This gift voucher has already been redeemed."

