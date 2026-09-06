"""
tests/unit/booking/test_state_machine.py
──────────────────────────────────────────
Unit tests for the Booking state machine.
"""

from __future__ import annotations

import pytest

from app.booking.domain.state_machine import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    BookingStateMachine,
)
from app.booking.domain.value_objects import BookingStatus
from app.core.exceptions import BookingStateError


class TestBookingStateMachine:

    def test_valid_transition_requested_to_payment_pending(self):
        machine = BookingStateMachine(BookingStatus.REQUESTED)
        result = machine.transition_to(BookingStatus.PAYMENT_PENDING)
        assert result == BookingStatus.PAYMENT_PENDING
        assert machine.current_status == BookingStatus.PAYMENT_PENDING

    def test_valid_transition_payment_pending_to_confirmed(self):
        machine = BookingStateMachine(BookingStatus.PAYMENT_PENDING)
        machine.transition_to(BookingStatus.CONFIRMED)
        assert machine.current_status == BookingStatus.CONFIRMED

    def test_invalid_transition_raises_error(self):
        machine = BookingStateMachine(BookingStatus.REQUESTED)
        with pytest.raises(BookingStateError):
            machine.transition_to(BookingStatus.COMPLETED)

    def test_terminal_state_cannot_transition(self):
        for terminal in TERMINAL_STATUSES:
            machine = BookingStateMachine(terminal)
            assert machine.is_terminal() is True
            with pytest.raises(BookingStateError):
                machine.transition_to(BookingStatus.REQUESTED)

    def test_can_transition_to_returns_false_for_invalid(self):
        machine = BookingStateMachine(BookingStatus.COMPLETED)
        assert machine.can_transition_to(BookingStatus.CONFIRMED) is False

    def test_can_transition_to_returns_true_for_valid(self):
        machine = BookingStateMachine(BookingStatus.REQUESTED)
        assert machine.can_transition_to(BookingStatus.PAYMENT_PENDING) is True

    def test_assert_valid_transition_static(self):
        # Should not raise
        BookingStateMachine.assert_valid_transition(
            BookingStatus.CONFIRMED, BookingStatus.CANCELLED
        )

    def test_all_states_covered_in_transition_table(self):
        """Ensure all BookingStatus values have an entry in the transition table."""
        for status in BookingStatus:
            assert status in VALID_TRANSITIONS, f"{status} not in VALID_TRANSITIONS"
