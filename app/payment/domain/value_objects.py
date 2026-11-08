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

    # Canonical values (see app.common.payment_attribution). Legacy names are kept as aliases of
    # the canonical member so existing references keep working.
    MYFATOORAH = "myfatoorah"
    PAYMENTLINK = "myfatoorah"
    DIRECTLINK = "myfatoorah"
    TAP = "tap"
    DEEMA = "tap"
    TAP_KNET = "tap"
    TAP_CARD = "tap"
    TAP_APPLE_PAY = "tap"
    KNET_POS = "knet_pos"
    KNET_CARD = "knet_pos"
    BOUBYAN = "boubyan"
    OTHER = "other"  # unclear provider: stored as NULL

    @classmethod
    def normalise(cls, value: str) -> "PaymentProvider":
        """Case-insensitive / alias normalisation to the canonical provider."""
        from app.common.payment_attribution import normalise_provider

        canonical = normalise_provider(value)
        if canonical is None:
            # historic behaviour: bare "knet" / "card" meant the KNET card terminal
            if (value or "").strip().lower() in {"knet", "card"}:
                return cls.KNET_POS
            return cls.OTHER
        return cls(canonical)


class PaymentThrough(str, Enum):
    """Channel through which payment was processed."""

    USHSPA = "ushspa"
    USHWEB = "ushweb"
    USHDESK = "ushdesk"
    DESK = "ushdesk"  # legacy alias of USHDESK
    OTHER = "other"  # unclear channel: stored as NULL

    @classmethod
    def normalise(cls, value: str) -> "PaymentThrough":
        from app.common.payment_attribution import normalise_through

        canonical = normalise_through(value)
        return cls(canonical) if canonical else cls.OTHER


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
    COURSE_SUBSCRIPTION = "course_subscription"

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
            "course_subscription": cls.COURSE_SUBSCRIPTION,
            "course": cls.COURSE_SUBSCRIPTION,
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

