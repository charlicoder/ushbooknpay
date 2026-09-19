"""
app/gifts/domain/value_objects.py
────────────────────────────────────
Gift Voucher V2 domain value objects and enumerations.
"""
from __future__ import annotations
from enum import Enum


class GiftType(str, Enum):
    DIGITAL = "DIGITAL"
    PHYSICAL = "PHYSICAL"
    SERVICE = "SERVICE"

    @property
    def requires_service(self) -> bool:
        return self in (GiftType.DIGITAL, GiftType.SERVICE)

    @property
    def forbids_service(self) -> bool:
        return self == GiftType.PHYSICAL


class GiftPurchaseStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    ACTIVE = "ACTIVE"
    CLAIMED = "CLAIMED"
    REDEEMED = "REDEEMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            GiftPurchaseStatus.REDEEMED,
            GiftPurchaseStatus.CANCELLED,
            GiftPurchaseStatus.EXPIRED,
        )


class GiftDeliveryStatus(str, Enum):
    PROCESSING = "PROCESSING"
    READY_FOR_DELIVERY = "READY_FOR_DELIVERY"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    RECEIVED = "RECEIVED"


class GiftCartStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CHECKED_OUT = "CHECKED_OUT"
    ABANDONED = "ABANDONED"
