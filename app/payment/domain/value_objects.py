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
    PAYMENTLINK = "PaymentLink"
    DIRECTLINK = "DirectLink"
    DEEMA = "Deema"
    KNET_CARD = "KNET Card"
    TAP_KNET = "Tap KNET"
    TAP_CARD = "Tap Card"
    TAP_APPLE_PAY = "Tap Apple Pay"
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
            "paymentlink": cls.PAYMENTLINK,
            "directlink": cls.DIRECTLINK,
            "direct": cls.DIRECTLINK,
            "deema": cls.DEEMA,
            "deemapaymentlink": cls.DEEMA,
            "deemalink": cls.DEEMA,
            "tapdeema": cls.DEEMA,
            "tapknet": cls.TAP_KNET,
            "tapcard": cls.TAP_CARD,
            "tapapplepay": cls.TAP_APPLE_PAY,
            "knetcard": cls.KNET_CARD,
            "knet": cls.KNET_CARD,
            "card": cls.KNET_CARD,
            "other": cls.OTHER,
        }
        return mapping.get(v, cls.OTHER)


class PaymentThrough(str, Enum):
    """Channel through which payment was processed."""

    USHSPA = "ushspa"
    USHWEB = "ushweb"
    USHDESK = "ushdesk"
    DESK = "desk"
    OTHER = "other"

    @classmethod
    def normalise(cls, value: str) -> "PaymentThrough":
        if not value:
            return cls.OTHER
        v = value.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        mapping = {
            "ushspa": cls.USHSPA,
            "app": cls.USHSPA,
            "ushweb": cls.USHWEB,
            "web": cls.USHWEB,
            "ushdesk": cls.USHDESK,
            "desk": cls.USHDESK,
            "pos": cls.USHDESK,
            "counter": cls.USHDESK,
            "other": cls.OTHER,
        }
        return mapping.get(v, cls.OTHER)


class PaymentGateway(str, Enum):
    """Underlying payment gateway / network used for the transaction."""

    KNET = "KNET"
    TAP = "TAP"
    DEEMA = "Deema"
    OTHER = "Other"

    @classmethod
    def normalise(cls, value: str) -> "PaymentGateway":
        if not value:
            return cls.OTHER
        v = value.strip().upper().replace("-", "").replace("_", "").replace(" ", "")
        mapping = {
            "KNET": cls.KNET,
            "TAP": cls.TAP,
            "DEEMA": cls.DEEMA,
            "DEEMAPAYMENTLINK": cls.DEEMA,
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
    DEEMA = "deema"
    PAYMENT_LINK = "payment_link"
    CASH = "cash"
    UNKNOWN = "unknown"


class RefundType(str, Enum):
    """Refund workflow classification."""

    AUTOMATED = "automated"
    MANUAL = "manual"

    @classmethod
    def normalise(cls, value: str) -> "RefundType":
        if not value:
            return cls.MANUAL
        v = value.strip().lower()
        if "auto" in v or "gateway" in v:
            return cls.AUTOMATED
        return cls.MANUAL


class RefundMethod(str, Enum):
    """Payment method/channel used to return funds to customer."""

    PAYMENT_GATEWAY = "payment_gateway"
    PAYMENT_LINK = "payment_link"
    CASH = "cash"
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    LOYALTY_POINTS = "loyalty_points"
    MONEY = "money"
    OTHER = "other"

    @classmethod
    def normalise(cls, value: str) -> "RefundMethod":
        if not value:
            return cls.OTHER
        v = value.strip().lower().replace("-", "_").replace(" ", "_")
        mapping = {
            "payment_gateway": cls.PAYMENT_GATEWAY,
            "gateway": cls.PAYMENT_GATEWAY,
            "tap": cls.PAYMENT_GATEWAY,
            "deema": cls.PAYMENT_GATEWAY,
            "myfatoorah": cls.PAYMENT_GATEWAY,
            "knet": cls.PAYMENT_GATEWAY,
            "payment_link": cls.PAYMENT_LINK,
            "deema_paymentlink": cls.PAYMENT_LINK,
            "link": cls.PAYMENT_LINK,
            "cash": cls.CASH,
            "card": cls.CARD,
            "pos": cls.CARD,
            "credit_card": cls.CARD,
            "debit_card": cls.CARD,
            "bank_transfer": cls.BANK_TRANSFER,
            "bank": cls.BANK_TRANSFER,
            "transfer": cls.BANK_TRANSFER,
            "loyalty_points": cls.LOYALTY_POINTS,
            "loyalty": cls.LOYALTY_POINTS,
            "points": cls.LOYALTY_POINTS,
            "money": cls.MONEY,
            "other": cls.OTHER,
        }
        return mapping.get(v, cls.OTHER)


class RefundStatus(str, Enum):
    """Refund transaction lifecycle states."""

    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

