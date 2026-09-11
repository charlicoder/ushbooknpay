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
    """Supported payment providers / gateways used to process the transaction."""

    MYFATOORAH = "MyFatoorah"
    DIRECTLINK = "DirectLink"
    DEEMA = "Deema"
    OTHER = "Other"

    @classmethod
    def normalise(cls, value: str) -> "PaymentProvider":
        """Case-insensitive / alias normalisation."""
        if not value:
            return cls.OTHER
        v = value.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        mapping = {
            "myfatoorah": cls.MYFATOORAH,
            "myfatora": cls.MYFATOORAH,
            "fatoorah": cls.MYFATOORAH,
            "directlink": cls.DIRECTLINK,
            "direct": cls.DIRECTLINK,
            "deema": cls.DEEMA,
            "other": cls.OTHER,
        }
        return mapping.get(v, cls.OTHER)


class PaymentThrough(str, Enum):
    """Channel through which payment was processed."""

    USHSPA = "ushspa"
    DESK = "desk"
    OTHER = "other"

    @classmethod
    def normalise(cls, value: str) -> "PaymentThrough":
        if not value:
            return cls.OTHER
        v = value.strip().lower()
        mapping = {
            "ushspa": cls.USHSPA,
            "app": cls.USHSPA,
            "desk": cls.DESK,
            "pos": cls.DESK,
            "counter": cls.DESK,
            "other": cls.OTHER,
        }
        return mapping.get(v, cls.OTHER)


class PaymentGateway(str, Enum):
    """Underlying payment gateway / network used for the transaction."""

    KNET = "KNET"
    TAP = "TAP"
    OTHER = "Other"

    @classmethod
    def normalise(cls, value: str) -> "PaymentGateway":
        if not value:
            return cls.OTHER
        v = value.strip().upper()
        mapping = {
            "KNET": cls.KNET,
            "K-NET": cls.KNET,
            "TAP": cls.TAP,
            "OTHER": cls.OTHER,
        }
        return mapping.get(v, cls.OTHER)


class PaymentFor(str, Enum):
    """Purpose or target entity for which the payment is made."""

    BRANCH_SERVICE = "branch_service"
    HOME_SERVICE = "home_service"
    GIFT_VOUCHER = "gift_voucher"
    PRODUCT_ITEMS = "product_items"

    @classmethod
    def normalise(cls, value: str) -> "PaymentFor":
        if not value:
            return cls.BRANCH_SERVICE
        v = value.strip().lower().replace("-", "_").replace(" ", "_")
        mapping = {
            "branch_service": cls.BRANCH_SERVICE,
            "service": cls.BRANCH_SERVICE,
            "home_service": cls.HOME_SERVICE,
            "home": cls.HOME_SERVICE,
            "gift_voucher": cls.GIFT_VOUCHER,
            "voucher": cls.GIFT_VOUCHER,
            "product_items": cls.PRODUCT_ITEMS,
            "products": cls.PRODUCT_ITEMS,
            "product": cls.PRODUCT_ITEMS,
        }
        return mapping.get(v, cls.BRANCH_SERVICE)


class PaymentMethod(str, Enum):
    """Customer-facing payment method."""

    CARD = "card"
    KNET = "knet"
    APPLE_PAY = "apple_pay"
    GOOGLE_PAY = "google_pay"
    CASH = "cash"
    UNKNOWN = "unknown"
