"""
app/booking/domain/state_machine.py
─────────────────────────────────────
Booking status state machine with explicit transition table.

Design:
- All valid transitions are explicitly enumerated.
- Attempting an invalid transition raises BookingStateError.
- Terminal states (CANCELLED, COMPLETED, NO_SHOW) cannot be left.
- This module contains zero I/O — it is pure business logic.
"""

from __future__ import annotations

from app.booking.domain.value_objects import BookingStatus
from app.core.exceptions import BookingStateError

# ── Transition Table ──────────────────────────────────────────────────────────
# Maps: current_status -> set of valid next statuses

VALID_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.REQUESTED: frozenset(
        {BookingStatus.PAYMENT_PENDING, BookingStatus.CONFIRMED, BookingStatus.PAYMENT_FAILED, BookingStatus.CANCELLED}
    ),
    BookingStatus.PAYMENT_PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.PAYMENT_FAILED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CONFIRMED: frozenset(
        {
            BookingStatus.RESCHEDULE_REQUESTED,
            BookingStatus.CANCELLED,
            BookingStatus.COMPLETED,
            BookingStatus.NO_SHOW,
        }
    ),
    BookingStatus.PAYMENT_FAILED: frozenset(
        {BookingStatus.PAYMENT_PENDING, BookingStatus.CANCELLED}
    ),
    BookingStatus.RESCHEDULE_REQUESTED: frozenset(
        {
            BookingStatus.RESCHEDULE_APPROVED,
            BookingStatus.CONFIRMED,   # reschedule denied → revert to confirmed
            BookingStatus.CANCELLED,
        }
    ),
    BookingStatus.RESCHEDULE_APPROVED: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.CANCELLED}
    ),
    # Terminal states — no valid transitions
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.NO_SHOW: frozenset(),
}

TERMINAL_STATUSES: frozenset[BookingStatus] = frozenset(
    {BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.NO_SHOW}
)


class BookingStateMachine:
    """
    Validates and enforces booking status transitions.

    Usage::

        machine = BookingStateMachine(current_status=BookingStatus.REQUESTED)
        machine.transition_to(BookingStatus.PAYMENT_PENDING)  # OK
        machine.transition_to(BookingStatus.COMPLETED)        # raises BookingStateError
    """

    def __init__(self, current_status: BookingStatus) -> None:
        self._status = current_status

    @property
    def current_status(self) -> BookingStatus:
        return self._status

    def can_transition_to(self, target: BookingStatus) -> bool:
        """Return True if the transition is valid without raising."""
        allowed = VALID_TRANSITIONS.get(self._status, frozenset())
        return target in allowed

    def transition_to(self, target: BookingStatus) -> BookingStatus:
        """
        Perform the transition and return the new status.

        Raises:
            BookingStateError: if the transition is not permitted.
        """
        if not self.can_transition_to(target):
            allowed = VALID_TRANSITIONS.get(self._status, frozenset())
            raise BookingStateError(
                f"Cannot transition booking from '{self._status.value}' "
                f"to '{target.value}'. "
                f"Allowed transitions: {sorted(s.value for s in allowed) or 'none (terminal state)'}.",
                code="INVALID_BOOKING_STATE_TRANSITION",
            )
        self._status = target
        return self._status

    @staticmethod
    def assert_valid_transition(
        current: BookingStatus,
        target: BookingStatus,
    ) -> None:
        """
        Class-level helper to validate a transition without maintaining state.

        Raises BookingStateError if the transition is invalid.
        """
        machine = BookingStateMachine(current)
        machine.transition_to(target)

    def is_terminal(self) -> bool:
        """Return True if the current status is a terminal state."""
        return self._status in TERMINAL_STATUSES
