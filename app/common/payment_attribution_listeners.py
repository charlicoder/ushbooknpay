"""
app/common/payment_attribution_listeners.py
───────────────────────────────────────────
SQLAlchemy mapper events that enforce the canonical payment attribution
(``payment_through`` / ``payment_provider`` / ``payment_method``) on EVERY insert / update of
bookings, payments, gift vouchers and shop orders, regardless of which code path wrote them
(API, ushnotice, webhooks, payment-link settlement, scripts).

See :mod:`app.common.payment_attribution` for the rules.
"""

from __future__ import annotations

from sqlalchemy import event

from app.common.payment_attribution import apply_attribution, apply_branch_name

_registered = False


def _before_write(_mapper, _connection, target) -> None:  # noqa: ANN001
    apply_attribution(target)
    apply_branch_name(target)


def register_payment_attribution_listeners() -> None:
    """Idempotently attach the listeners to the payment-bearing models."""
    global _registered
    if _registered:
        return

    from app.booking.infrastructure.models import Booking
    from app.payment.infrastructure.models import Payment
    from app.shop.infrastructure.models import ShopOrder
    from app.voucher.infrastructure.models import GiftVoucher

    for model in (Booking, Payment, GiftVoucher, ShopOrder):
        event.listen(model, "before_insert", _before_write)
        event.listen(model, "before_update", _before_write)
    _registered = True

