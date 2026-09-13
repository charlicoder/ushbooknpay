"""
app/shop/domain/state_machine.py
──────────────────────────────────
DeliveryStateMachine — enforces valid delivery status transitions.

Open/Closed: add new states by extending TRANSITIONS without modifying
existing transition logic.
"""

from __future__ import annotations

from app.shop.domain.value_objects import DeliveryStatus

# Maps each status to the set of statuses it may transition to.
# Only delivery agents may drive: ORDERED → READY_TO_GO → ON_THE_WAY → DELIVERED
# Only the customer (with secret code) may drive: DELIVERED → RECEIVED
TRANSITIONS: dict[DeliveryStatus, set[DeliveryStatus]] = {
    DeliveryStatus.ORDERED: {DeliveryStatus.READY_TO_GO},
    DeliveryStatus.READY_TO_GO: {DeliveryStatus.ON_THE_WAY},
    DeliveryStatus.ON_THE_WAY: {DeliveryStatus.DELIVERED},
    DeliveryStatus.DELIVERED: {DeliveryStatus.RECEIVED},
    DeliveryStatus.RECEIVED: set(),  # terminal
}

# Transitions that require the customer secret code
CUSTOMER_ONLY_TRANSITIONS: set[tuple[DeliveryStatus, DeliveryStatus]] = {
    (DeliveryStatus.DELIVERED, DeliveryStatus.RECEIVED),
}


class DeliveryStateMachine:
    """
    Validates delivery status transitions.

    Usage::

        machine = DeliveryStateMachine(current_status)
        machine.assert_can_transition(new_status)          # raises on invalid
        machine.assert_staff_can_transition(new_status)    # also rejects customer-only
    """

    def __init__(self, current: DeliveryStatus) -> None:
        self._current = current

    def can_transition(self, to: DeliveryStatus) -> bool:
        return to in TRANSITIONS.get(self._current, set())

    def assert_can_transition(self, to: DeliveryStatus) -> None:
        if not self.can_transition(to):
            raise InvalidDeliveryTransitionError(
                f"Cannot transition from '{self._current}' to '{to}'."
            )

    def assert_staff_can_transition(self, to: DeliveryStatus) -> None:
        self.assert_can_transition(to)
        if (self._current, to) in CUSTOMER_ONLY_TRANSITIONS:
            raise InvalidDeliveryTransitionError(
                f"Transition '{self._current}' → '{to}' can only be done by "
                "the customer using their secret tracking code."
            )

    def assert_customer_can_transition(self, to: DeliveryStatus) -> None:
        self.assert_can_transition(to)
        if (self._current, to) not in CUSTOMER_ONLY_TRANSITIONS:
            raise InvalidDeliveryTransitionError(
                f"Transition '{self._current}' → '{to}' is not available to customers."
            )


class InvalidDeliveryTransitionError(Exception):
    """Raised when a delivery status transition is not allowed."""
