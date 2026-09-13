"""
app/shop/domain/value_objects.py
─────────────────────────────────
Shop domain enumerations and multilingual label maps.
"""

from __future__ import annotations

from enum import Enum


class DeliveryStatus(str, Enum):
    """Order delivery lifecycle states."""

    ORDERED = "ordered"
    READY_TO_GO = "ready_to_go"
    ON_THE_WAY = "on_the_way"
    DELIVERED = "delivered"
    RECEIVED = "received"


class OrderPaymentStatus(str, Enum):
    """Payment lifecycle for a shop order."""

    NOT_INITIATED = "not_initiated"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"


# ── Multilingual status labels ────────────────────────────────────────────────

DELIVERY_STATUS_LABELS: dict[str, dict[str, str]] = {
    "en": {
        DeliveryStatus.ORDERED: "Ordered",
        DeliveryStatus.READY_TO_GO: "Ready To Go",
        DeliveryStatus.ON_THE_WAY: "On The Way",
        DeliveryStatus.DELIVERED: "Delivered",
        DeliveryStatus.RECEIVED: "Received",
    },
    "ar": {
        DeliveryStatus.ORDERED: "تم الطلب",
        DeliveryStatus.READY_TO_GO: "جاهز للإرسال",
        DeliveryStatus.ON_THE_WAY: "في الطريق",
        DeliveryStatus.DELIVERED: "تم التسليم",
        DeliveryStatus.RECEIVED: "تم الاستلام",
    },
}


def get_status_label(status: DeliveryStatus, lang: str = "en") -> str:
    """Return the human-readable label for a delivery status in the given language."""
    return DELIVERY_STATUS_LABELS.get(lang, DELIVERY_STATUS_LABELS["en"]).get(
        status, status.value
    )
