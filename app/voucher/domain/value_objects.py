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


class VoucherPaymentThrough(str, Enum):
    """
    Channel through which the voucher was sold.

    ushspa  — sold via the USHSPA mobile app or web platform
    desk    — sold at the spa reception / front desk (cash/POS)
    """

    USHSPA = "ushspa"
    DESK = "desk"
