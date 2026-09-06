"""
app/booking/domain/value_objects.py
─────────────────────────────────────
Booking domain value objects and enumerations.

Value objects are immutable and compared by value, not identity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


# ── Enumerations ─────────────────────────────────────────────────────────────


class BookingStatus(str, Enum):
    """
    Booking lifecycle states.

    State transitions are enforced by BookingStateMachine.
    """

    REQUESTED = "requested"
    PAYMENT_PENDING = "payment_pending"
    CONFIRMED = "confirmed"
    PAYMENT_FAILED = "payment_failed"
    RESCHEDULE_REQUESTED = "reschedule_requested"
    RESCHEDULE_APPROVED = "reschedule_approved"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class PaymentStatus(str, Enum):
    """Payment lifecycle states within the booking context."""

    NOT_INITIATED = "not_initiated"
    INITIATED = "initiated"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    REWARDED = "rewarded"
    PARTIALLY_REFUNDED = "partially_refunded"


class BookingType(str, Enum):
    """Whether the booking is at a branch or at the customer's home."""

    BRANCH = "branch"
    HOME = "home"
    LOYALTY = "loyalty"
    GIFT_VOUCHER = "gift_voucher"


class ServiceType(str, Enum):
    """Whether the booking is at a branch or at the customer's home."""

    BRANCH = "branch"
    HOME = "home"


class PaymentProvider(str, Enum):
    """Supported payment providers."""

    MYFATOORAH = "myfatoorah"
    TAP = "tap"


# ── Value Objects ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeSlot:
    """An immutable time window for an appointment."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("TimeSlot start must be before end.")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeSlot datetimes must be timezone-aware.")

    @property
    def duration_minutes(self) -> int:
        delta = self.end - self.start
        return int(delta.total_seconds() / 60)

    def overlaps(self, other: "TimeSlot") -> bool:
        """Return True if this slot overlaps with *other*."""
        return self.start < other.end and self.end > other.start


@dataclass(frozen=True)
class Money:
    """
    Immutable monetary amount.

    Always uses Decimal to avoid floating-point errors.
    """

    amount: Decimal
    currency: str = "KWD"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
        if len(self.currency) != 3:
            raise ValueError(f"Currency must be ISO 4217 3-letter code, got: {self.currency!r}")

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError("Cannot add Money with different currencies.")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError("Cannot subtract Money with different currencies.")
        return Money(self.amount - other.amount, self.currency)

    @classmethod
    def zero(cls, currency: str = "KWD") -> "Money":
        return cls(Decimal("0.000"), currency)

    def __str__(self) -> str:
        return f"{self.amount:.3f} {self.currency}"


@dataclass(frozen=True)
class PricingBreakdown:
    """
    Complete pricing breakdown for a booking.

    All amounts are Decimal to maintain precision.
    Base service price is excluded as it is overridden by arrangement price.
    """

    arrangement_price: Decimal
    price_for_extra_minutes: Decimal
    addon_price: Decimal
    discount: Decimal
    tax: Decimal
    fees: Decimal
    total: Decimal
    currency: str

    def __post_init__(self) -> None:
        for fname in (
            "arrangement_price", "price_for_extra_minutes", "addon_price",
            "discount", "tax", "fees", "total",
        ):
            val = getattr(self, fname)
            if not isinstance(val, Decimal):
                object.__setattr__(self, fname, Decimal(str(val)))

    @classmethod
    def calculate(
        cls,
        *,
        arrangement_price: Decimal,
        price_for_extra_minutes: Decimal = Decimal("0.000"),
        addon_price: Decimal = Decimal("0.000"),
        discount: Decimal = Decimal("0.000"),
        tax_rate: Decimal = Decimal("0.000"),
        fees: Decimal = Decimal("0.000"),
        currency: str = "KWD",
    ) -> "PricingBreakdown":
        """
        Calculate the total from components.

        total = (arrangement_price + price_for_extra_minutes + addon_price - discount) * (1 + tax_rate) + fees
        """
        subtotal = arrangement_price + price_for_extra_minutes + addon_price - discount
        tax = (subtotal * tax_rate).quantize(Decimal("0.001"))
        total = (subtotal + tax + fees).quantize(Decimal("0.001"))

        return cls(
            arrangement_price=arrangement_price.quantize(Decimal("0.001")),
            price_for_extra_minutes=price_for_extra_minutes.quantize(Decimal("0.001")),
            addon_price=addon_price.quantize(Decimal("0.001")),
            discount=discount.quantize(Decimal("0.001")),
            tax=tax,
            fees=fees.quantize(Decimal("0.001")),
            total=total,
            currency=currency,
        )
