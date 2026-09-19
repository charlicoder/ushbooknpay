"""
app/gifts/domain/state_machine.py
──────────────────────────────────
Gift Purchase state machine.
"""
from __future__ import annotations

from app.gifts.domain.value_objects import GiftPurchaseStatus

_TRANSITIONS: dict[GiftPurchaseStatus, frozenset[GiftPurchaseStatus]] = {
    GiftPurchaseStatus.PENDING_PAYMENT: frozenset({
        GiftPurchaseStatus.PAID,
        GiftPurchaseStatus.ACTIVE,
        GiftPurchaseStatus.CANCELLED,
    }),
    GiftPurchaseStatus.PAID: frozenset({
        GiftPurchaseStatus.ACTIVE,
        GiftPurchaseStatus.CANCELLED,
    }),
    GiftPurchaseStatus.ACTIVE: frozenset({
        GiftPurchaseStatus.CLAIMED,
        GiftPurchaseStatus.REDEEMED,
        GiftPurchaseStatus.EXPIRED,
        GiftPurchaseStatus.CANCELLED,
    }),
    GiftPurchaseStatus.CLAIMED: frozenset({
        GiftPurchaseStatus.REDEEMED,
        GiftPurchaseStatus.EXPIRED,
        GiftPurchaseStatus.CANCELLED,
    }),
    GiftPurchaseStatus.REDEEMED: frozenset(),
    GiftPurchaseStatus.CANCELLED: frozenset(),
    GiftPurchaseStatus.EXPIRED: frozenset(),
}

class GiftPurchaseStateMachine:
    @staticmethod
    def allowed_transitions(current: GiftPurchaseStatus) -> frozenset[GiftPurchaseStatus]:
        return _TRANSITIONS.get(current, frozenset())

    @staticmethod
    def validate_transition(current: GiftPurchaseStatus, new: GiftPurchaseStatus) -> None:
        if current == new:
            return
        allowed = _TRANSITIONS.get(current, frozenset())
        if new not in allowed:
            raise ValueError(f"Invalid transition")
