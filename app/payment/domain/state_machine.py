"""
app/payment/domain/state_machine.py
─────────────────────────────────────
Payment status state machine with explicit transition table.
"""

from __future__ import annotations

from app.core.exceptions import PaymentStateError
from app.payment.domain.value_objects import PaymentTransactionStatus

VALID_TRANSITIONS: dict[PaymentTransactionStatus, frozenset[PaymentTransactionStatus]] = {
    PaymentTransactionStatus.INITIATED: frozenset(
        {
            PaymentTransactionStatus.PENDING,
            PaymentTransactionStatus.FAILED,
            PaymentTransactionStatus.CANCELLED,
        }
    ),
    PaymentTransactionStatus.PENDING: frozenset(
        {
            PaymentTransactionStatus.SUCCESS,
            PaymentTransactionStatus.FAILED,
            PaymentTransactionStatus.CANCELLED,
        }
    ),
    PaymentTransactionStatus.SUCCESS: frozenset(
        {
            PaymentTransactionStatus.REFUNDED,
            PaymentTransactionStatus.PARTIALLY_REFUNDED,
        }
    ),
    PaymentTransactionStatus.FAILED: frozenset(
        {PaymentTransactionStatus.PENDING}  # allow retry
    ),
    PaymentTransactionStatus.CANCELLED: frozenset(),
    PaymentTransactionStatus.REFUNDED: frozenset(),
    PaymentTransactionStatus.PARTIALLY_REFUNDED: frozenset(
        {PaymentTransactionStatus.REFUNDED}
    ),
}

TERMINAL_PAYMENT_STATUSES: frozenset[PaymentTransactionStatus] = frozenset(
    {
        PaymentTransactionStatus.CANCELLED,
        PaymentTransactionStatus.REFUNDED,
    }
)


class PaymentStateMachine:
    """Validates and enforces payment status transitions."""

    def __init__(self, current_status: PaymentTransactionStatus) -> None:
        self._status = current_status

    @property
    def current_status(self) -> PaymentTransactionStatus:
        return self._status

    def can_transition_to(self, target: PaymentTransactionStatus) -> bool:
        return target in VALID_TRANSITIONS.get(self._status, frozenset())

    def transition_to(self, target: PaymentTransactionStatus) -> PaymentTransactionStatus:
        if not self.can_transition_to(target):
            allowed = VALID_TRANSITIONS.get(self._status, frozenset())
            raise PaymentStateError(
                f"Cannot transition payment from '{self._status.value}' "
                f"to '{target.value}'. "
                f"Allowed: {sorted(s.value for s in allowed) or 'none (terminal)'}.",
                code="INVALID_PAYMENT_STATE_TRANSITION",
            )
        self._status = target
        return self._status

    @staticmethod
    def assert_valid_transition(
        current: PaymentTransactionStatus,
        target: PaymentTransactionStatus,
    ) -> None:
        PaymentStateMachine(current).transition_to(target)

    def is_terminal(self) -> bool:
        return self._status in TERMINAL_PAYMENT_STATUSES
