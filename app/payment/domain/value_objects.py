"""
app/payment/domain/value_objects.py
─────────────────────────────────────
Payment domain value objects and enumerations.
"""

from __future__ import annotations

from enum import Enum


class PaymentTransactionStatus(str, Enum):
    """Payment record lifecycle states."""

    INITIATED = "initiated"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class PaymentProvider(str, Enum):
    """Supported payment gateways."""

    MYFATOORAH = "myfatoorah"
    TAP = "tap"


class PaymentMethod(str, Enum):
    """Customer-facing payment methods."""

    CARD = "card"
    KNET = "knet"
    APPLE_PAY = "apple_pay"
    GOOGLE_PAY = "google_pay"
    UNKNOWN = "unknown"


class PaymentFor(str, Enum):
    """Purpose or target entity for which the payment is made."""

    GIFT_VOUCHER = "gift_voucher"
    SERVICE = "service"
    HOME_SERVICE = "home_service"
    PRODUCTS = "products"
    LOYALTY = "loyalty"
    OTHERS = "others"
