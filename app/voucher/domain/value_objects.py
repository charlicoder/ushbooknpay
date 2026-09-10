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
    active          — payment confirmed; voucher is valid and can be redeemed
    redeemed        — recipient has used the voucher to book a service
    fulfilled       — the booked appointment has been completed
    expired         — voucher passed its expire_date without redemption
    cancelled       — manually cancelled (admin or sender)
    """

    CREATED = "created"
    PAYMENT_PENDING = "payment_pending"
    ACTIVE = "active"
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


class VoucherPaymentProvider(str, Enum):
    """
    Payment gateway / provider used to process the voucher purchase.

    MyFatoorah  — Kuwait-based payment aggregator (KNET, Visa, MasterCard, etc.)
    DirectLink  — Direct bank link / KNET direct integration
    Deema       — Deema BNPL (Buy Now Pay Later) provider
    Other       — Any other provider not listed above
    """

    MYFATOORAH = "MyFatoorah"
    DIRECTLINK = "DirectLink"
    DEEMA = "Deema"
    OTHER = "Other"

    @classmethod
    def normalise(cls, val: str | None) -> str | None:
        """
        Normalise provider string to canonical PascalCase value.
        Handles case insensitivity ('directlink' -> 'DirectLink'),
        separators ('direct_link' -> 'DirectLink'), and common aliases ('myfatora' -> 'MyFatoorah').
        """
        if not val or not str(val).strip():
            return None
        cleaned = str(val).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
        lookup = {
            "myfatoorah": cls.MYFATOORAH.value,
            "myfatora": cls.MYFATOORAH.value,
            "fatoorah": cls.MYFATOORAH.value,
            "directlink": cls.DIRECTLINK.value,
            "direct": cls.DIRECTLINK.value,
            "deema": cls.DEEMA.value,
            "other": cls.OTHER.value,
        }
        if cleaned in lookup:
            return lookup[cleaned]
        for member in cls:
            if member.value.lower() == cleaned:
                return member.value
        return str(val).strip()


class VoucherPaymentThrough(str, Enum):
    """
    Channel through which the voucher was sold.

    ushspa  — sold via the USHSPA mobile app or web platform
    desk    — sold at the spa reception / front desk (cash/POS)
    """

    USHSPA = "ushspa"
    DESK = "desk"

    @classmethod
    def normalise(cls, val: str | None) -> str | None:
        """
        Normalise payment_through string to canonical value ('ushspa' or 'desk').
        Handles case insensitivity ('DESK' -> 'desk') and common aliases ('app' -> 'ushspa').
        """
        if not val or not str(val).strip():
            return None
        cleaned = str(val).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
        lookup = {
            "ushspa": cls.USHSPA.value,
            "app": cls.USHSPA.value,
            "web": cls.USHSPA.value,
            "desk": cls.DESK.value,
            "pos": cls.DESK.value,
            "reception": cls.DESK.value,
            "frontdesk": cls.DESK.value,
        }
        if cleaned in lookup:
            return lookup[cleaned]
        for member in cls:
            if member.value.lower() == cleaned:
                return member.value
        return str(val).strip()
