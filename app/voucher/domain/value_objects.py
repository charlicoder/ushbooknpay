"""
app/voucher/domain/value_objects.py
─────────────────────────────────────
Gift Voucher domain value objects and enumerations.
"""

from __future__ import annotations

from enum import Enum


class GiftVoucherStatus(str, Enum):
    """
    Gift voucher lifecycle states.

    Transitions are enforced by GiftVoucherStateMachine.

    created         — record created, awaiting payment
    payment_pending — payment initiated but not yet confirmed
    scheduled       — payment confirmed, delivery scheduled for a future datetime;
                      cannot be redeemed until the scheduler activates it
    active          — payment confirmed; voucher is valid and can be redeemed
    partially_redeemed — some (not all) gifted services were used in one or more bookings
    redeemed        — recipient has used the voucher to book a service (all services used)
    fulfilled       — the booked appointment has been completed
    expired         — voucher passed its expire_date without redemption
    cancelled       — manually cancelled (admin or sender)
    """

    CREATED = "created"
    PAYMENT_PENDING = "payment_pending"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PARTIALLY_REDEEMED = "partially_redeemed"
    REDEEMED = "redeemed"
    FULFILLED = "fulfilled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Return True if no further transitions are possible."""
        return self in (
            GiftVoucherStatus.FULFILLED,
            GiftVoucherStatus.EXPIRED,
            GiftVoucherStatus.CANCELLED,
        )

    @classmethod
    def normalise(cls, val: str | None) -> str | None:
        """Normalise input string to a valid status value, or return original/None."""
        if not val or not str(val).strip():
            return None
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned:
                return member.value
        return str(val).strip()


class VoucherDeliveryMode(str, Enum):
    """
    When the recipient is notified / the voucher becomes usable.

    on_activation — immediately after successful payment (default)
    scheduled     — at ``scheduled_delivery_at`` (Kuwait time) via EventBridge Scheduler
    """

    ON_ACTIVATION = "on_activation"
    SCHEDULED = "scheduled"

    @classmethod
    def normalise(cls, val: str | None) -> str:
        cleaned = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
        return cls.SCHEDULED.value if cleaned == "scheduled" else cls.ON_ACTIVATION.value


class GiftCategory(str, Enum):
    """
    Gift voucher category classification.

    digital  — Digital gift voucher
    physical — Physical gift voucher
    service  — Service gift voucher
    """

    DIGITAL = "digital"
    PHYSICAL = "physical"
    SERVICE = "service"

    @classmethod
    def normalise(cls, val: str | None) -> str:
        """Normalise input string to a valid GiftCategory value, default to 'service'."""
        if not val or not str(val).strip():
            return cls.SERVICE.value
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned:
                return member.value
        return cleaned


class VoucherPaymentProvider(str, Enum):
    """
    Payment gateway / provider used to process the voucher purchase.

    MyFatoorah  — Kuwait-based payment aggregator (KNET, Visa, MasterCard, etc.)
    DirectLink  — Direct bank link / KNET direct integration
    Deema       — Deema BNPL (Buy Now Pay Later) provider
    Other       — Any other provider not listed above
    """

    # Canonical values (see app.common.payment_attribution); legacy names are aliases.
    MYFATOORAH = "myfatoorah"
    DIRECTLINK = "myfatoorah"
    TAP = "tap"
    DEEMA = "tap"
    KNET_POS = "knet_pos"
    BOUBYAN = "boubyan"
    OTHER = "other"  # unclear provider: stored as NULL

    @classmethod
    def normalise(cls, val: str | None) -> str | None:
        """Normalise to the canonical provider (myfatoorah | tap | knet_pos | boubyan)."""
        from app.common.payment_attribution import normalise_provider

        if not val or not str(val).strip():
            return None
        canonical = normalise_provider(val)
        if canonical:
            return canonical
        if str(val).strip().lower() == "other":
            return cls.OTHER.value
        return str(val).strip()


class VoucherPaymentThrough(str, Enum):
    """
    Channel through which the voucher was sold.

    ushspa  — sold via the USHSPA mobile app
    ushweb  — sold via the USHSPA web platform
    desk    — sold at the spa reception / front desk (cash/POS)
    """

    USHSPA = "ushspa"
    USHWEB = "ushweb"
    USHDESK = "ushdesk"
    DESK = "ushdesk"  # legacy alias of USHDESK

    @classmethod
    def normalise(cls, val: str | None) -> str | None:
        """Normalise to the canonical channel ('ushspa' | 'ushweb' | 'ushdesk')."""
        from app.common.payment_attribution import normalise_through

        if not val or not str(val).strip():
            return None
        return normalise_through(val) or str(val).strip()


from app.shop.domain.value_objects import (
    DELIVERY_STATUS_LABELS,
    DeliveryStatus,
    get_status_label as get_delivery_status_label,
)
