"""
app/voucher/domain/state_machine.py
─────────────────────────────────────
Gift Voucher state machine.

Enforces valid status transitions and raises a domain exception on
any illegal transition attempt.

Valid transitions:
  created         → payment_pending | active | cancelled
  payment_pending → active | payment_pending | cancelled
  active          → redeemed | expired | cancelled
  redeemed        → fulfilled
  fulfilled       → (terminal)
  expired         → (terminal)
  cancelled       → (terminal)
"""

from __future__ import annotations

from app.voucher.domain.value_objects import GiftVoucherStatus


# Adjacency map: current_status → set of allowed next statuses
_TRANSITIONS: dict[GiftVoucherStatus, frozenset[GiftVoucherStatus]] = {
    GiftVoucherStatus.CREATED: frozenset({
        GiftVoucherStatus.PAYMENT_PENDING,
        GiftVoucherStatus.ACTIVE,
        GiftVoucherStatus.CANCELLED,
    }),
    GiftVoucherStatus.PAYMENT_PENDING: frozenset({
        GiftVoucherStatus.ACTIVE,
        GiftVoucherStatus.PAYMENT_PENDING,  # idempotent retry
        GiftVoucherStatus.CANCELLED,
    }),
    GiftVoucherStatus.ACTIVE: frozenset({
        GiftVoucherStatus.REDEEMED,
        GiftVoucherStatus.EXPIRED,
        GiftVoucherStatus.CANCELLED,
    }),
    GiftVoucherStatus.REDEEMED: frozenset({
        GiftVoucherStatus.FULFILLED,
    }),
    # Terminal states — no outgoing edges
    GiftVoucherStatus.FULFILLED: frozenset(),
    GiftVoucherStatus.EXPIRED: frozenset(),
    GiftVoucherStatus.CANCELLED: frozenset(),
}


class GiftVoucherStateMachine:
    """
    Pure domain service that enforces voucher status transitions.

    Raises:
        ValueError: when the requested transition is not allowed.
    """

    @staticmethod
    def allowed_transitions(current: GiftVoucherStatus) -> frozenset[GiftVoucherStatus]:
        """Return the set of statuses reachable from *current*."""
        return _TRANSITIONS.get(current, frozenset())

    @staticmethod
    def validate_transition(
        current: GiftVoucherStatus,
        new: GiftVoucherStatus,
    ) -> None:
        """
        Assert that transitioning from *current* to *new* is valid.

        Args:
            current: The voucher's present status.
            new:     The requested next status.

        Raises:
            ValueError: with a descriptive message if the transition is illegal.
        """
        if current == new:
            # Idempotent update — always allowed (caller may safely re-apply same status)
            return

        allowed = _TRANSITIONS.get(current, frozenset())
        if new not in allowed:
            raise ValueError(
                f"Invalid gift voucher status transition: "
                f"{current.value!r} → {new.value!r}. "
                f"Allowed: {[s.value for s in sorted(allowed, key=lambda s: s.value)] or 'none (terminal state)'}."
            )
