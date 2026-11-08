"""
app/common/payment_attribution.py
─────────────────────────────────
Single source of truth for HOW a payment is recorded on bookings, payments, gift vouchers
and shop orders, so reports / analytics can group them reliably.

Canonical values
----------------
payment_through   ushweb | ushspa | ushdesk
payment_provider  myfatoorah | tap | knet_pos | boubyan
payment_method    knet_card | apple_pay | deema_link | boubyan_link | gift_voucher

Rules
-----
* ushweb  → provider ``myfatoorah``; method ``knet_card`` or ``apple_pay`` (from the gateway response)
* ushspa  → provider ``myfatoorah``; method ``knet_card`` or ``apple_pay`` (from the gateway response)
* ushdesk → the provider the staff picked:
    - MyFatoorah    → ``myfatoorah``; method = what the customer chose on the link
                      (``knet_card`` or ``apple_pay``, read from the gateway response)
    - Deema         → ``tap``       ; method ``deema_link``
    - KNET Card     → ``knet_pos``  ; method ``knet_card``
    - Boubyan Link  → ``boubyan``   ; method ``boubyan_link``
* Values that do not describe a real payment (``Other``, ``on_branch``, ``pay_now``, ``unknown`` …)
  are stored as NULL instead of being kept as unclear labels. "Pay on branch" is a booking
  *option*, not a payment method: until the customer actually pays the fields stay NULL.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── Canonical values ──────────────────────────────────────────────────────────

THROUGH_USHWEB = "ushweb"
THROUGH_USHSPA = "ushspa"
THROUGH_USHDESK = "ushdesk"

PROVIDER_MYFATOORAH = "myfatoorah"
PROVIDER_TAP = "tap"
PROVIDER_KNET_POS = "knet_pos"
PROVIDER_BOUBYAN = "boubyan"

METHOD_KNET_CARD = "knet_card"
METHOD_APPLE_PAY = "apple_pay"
METHOD_DEEMA_LINK = "deema_link"
METHOD_BOUBYAN_LINK = "boubyan_link"
METHOD_GIFT_VOUCHER = "gift_voucher"
METHOD_COURSE_SUBSCRIPTION = "course_subscription"

THROUGH_VALUES = (THROUGH_USHWEB, THROUGH_USHSPA, THROUGH_USHDESK)
PROVIDER_VALUES = (PROVIDER_MYFATOORAH, PROVIDER_TAP, PROVIDER_KNET_POS, PROVIDER_BOUBYAN)
METHOD_VALUES = (
    METHOD_KNET_CARD,
    METHOD_APPLE_PAY,
    METHOD_DEEMA_LINK,
    METHOD_BOUBYAN_LINK,
    METHOD_GIFT_VOUCHER,
    METHOD_COURSE_SUBSCRIPTION,
)

PAID_STATUSES = frozenset({"success", "paid", "completed", "rewarded"})


def _key(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


# ── payment_through ───────────────────────────────────────────────────────────

_THROUGH = {
    "ushdesk": THROUGH_USHDESK, "desk": THROUGH_USHDESK, "pos": THROUGH_USHDESK,
    "counter": THROUGH_USHDESK, "reception": THROUGH_USHDESK, "frontdesk": THROUGH_USHDESK,
    "dashboard": THROUGH_USHDESK, "admin": THROUGH_USHDESK, "staff": THROUGH_USHDESK,
    "ushweb": THROUGH_USHWEB, "web": THROUGH_USHWEB, "website": THROUGH_USHWEB,
    "ushspa": THROUGH_USHSPA, "app": THROUGH_USHSPA, "mobile": THROUGH_USHSPA,
    "mobileapp": THROUGH_USHSPA, "ios": THROUGH_USHSPA, "android": THROUGH_USHSPA,
    "ushspaapp": THROUGH_USHSPA,
}


def normalise_through(value: Any) -> str | None:
    """``ushweb`` | ``ushspa`` | ``ushdesk`` or None when unknown."""
    return _THROUGH.get(_key(value))


# ── payment_provider ──────────────────────────────────────────────────────────

_PROVIDER = {
    "myfatoorah": PROVIDER_MYFATOORAH, "myfatora": PROVIDER_MYFATOORAH, "fatoorah": PROVIDER_MYFATOORAH,
    "paymentlink": PROVIDER_MYFATOORAH, "directlink": PROVIDER_MYFATOORAH, "direct": PROVIDER_MYFATOORAH,
    "deema": PROVIDER_TAP, "deemapaymentlink": PROVIDER_TAP, "deemalink": PROVIDER_TAP,
    "tapdeema": PROVIDER_TAP, "tap": PROVIDER_TAP, "tapknet": PROVIDER_TAP,
    "tapcard": PROVIDER_TAP, "tapapplepay": PROVIDER_TAP,
    "knetcard": PROVIDER_KNET_POS, "knetpos": PROVIDER_KNET_POS, "knetterminal": PROVIDER_KNET_POS,
    "boubyan": PROVIDER_BOUBYAN, "boubyanlink": PROVIDER_BOUBYAN,
}


def normalise_provider(value: Any) -> str | None:
    """``myfatoorah`` | ``tap`` | ``knet_pos`` | ``boubyan`` or None ('Other', 'on_branch', blank …)."""
    return _PROVIDER.get(_key(value))


# ── payment_method ────────────────────────────────────────────────────────────


def gateway_text(*sources: Any) -> str:
    """Flatten gateway names found in a payment response / free text into one lower-case string.

    MyFatoorah reports the chosen method in ``InvoiceTransactions[].PaymentGateway`` /
    ``PaymentMethod`` (e.g. "KNET", "Visa/Master", "Apple Pay").
    """
    parts: list[str] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 6 or obj is None:
            return
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in {
                    "paymentgateway", "payment_gateway", "paymentmethod", "payment_method",
                    "gateway", "source", "invoicetransactions", "transactions", "data", "transaction",
                }:
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    for s in sources:
        if isinstance(s, str):
            stripped = s.strip()
            if stripped[:1] in "{[":
                try:
                    walk(json.loads(stripped))
                    continue
                except ValueError:
                    pass
            parts.append(s)
        else:
            walk(s)
    return " ".join(parts).lower()


def normalise_method(
    value: Any = None,
    *,
    provider: str | None = None,
    gateway: Any = None,
    payment_data: Any = None,
    paid: bool = False,
) -> str | None:
    """
    Canonical ``payment_method``.

    ``provider`` must already be canonical. ``gateway`` / ``payment_data`` carry what the
    customer actually chose on the provider page; when the method cannot be derived yet (the
    customer has not paid) NULL is returned, never a guess.
    """
    text = gateway_text(value, gateway, payment_data)
    if "apple" in text:
        return METHOD_APPLE_PAY
    if "deema" in text:
        return METHOD_DEEMA_LINK
    if "boubyan" in text:
        return METHOD_BOUBYAN_LINK
    if "voucher" in text:
        return METHOD_GIFT_VOUCHER

    if provider == PROVIDER_TAP:
        return METHOD_DEEMA_LINK
    if provider == PROVIDER_BOUBYAN:
        return METHOD_BOUBYAN_LINK
    if provider == PROVIDER_KNET_POS:
        return METHOD_KNET_CARD
    if provider == PROVIDER_MYFATOORAH:
        # An explicit KNET / Visa / Master result means the customer chose the card page;
        # a generic label ("card") is only trusted once the payment succeeded.
        if re.search(r"knet|visa|master|mada", text):
            return METHOD_KNET_CARD
        return METHOD_KNET_CARD if paid else None
    return None


# ── apply to a model ──────────────────────────────────────────────────────────


def _is_paid(obj: Any) -> bool:
    for attr in ("payment_status", "status", "transaction_status"):
        v = str(getattr(obj, attr, "") or "").strip().lower()
        if v in PAID_STATUSES:
            return True
    return False


def apply_attribution(obj: Any) -> None:
    """Normalise payment_through / payment_provider / payment_method (+ gateway) on ``obj`` in place.

    Works on any model exposing some of those attributes (Booking, Payment, GiftVoucher, ShopOrder).
    """
    has = lambda name: hasattr(obj, name)  # noqa: E731

    if has("payment_through"):
        obj.payment_through = normalise_through(getattr(obj, "payment_through", None))

    provider = None
    if has("payment_provider"):
        raw_provider = getattr(obj, "payment_provider", None)
        provider = normalise_provider(raw_provider)
        if provider is None:
            # derive from gateway / method hints when the provider label itself is unclear
            hint = gateway_text(
                getattr(obj, "payment_gateway", None) if has("payment_gateway") else None,
                getattr(obj, "payment_method", None) if has("payment_method") else None,
            )
            if "boubyan" in hint:
                provider = PROVIDER_BOUBYAN
            elif "deema" in hint:
                provider = PROVIDER_TAP
        obj.payment_provider = provider

    if has("payment_method"):
        raw_method = getattr(obj, "payment_method", None)
        key = _key(raw_method)
        if key in METHOD_VALUES_KEYS:
            method: str | None = METHOD_BY_KEY[key]
        else:
            method = normalise_method(
                raw_method,
                provider=provider,
                gateway=getattr(obj, "payment_gateway", None) if has("payment_gateway") else None,
                payment_data=getattr(obj, "payment_data", None) if has("payment_data") else None,
                paid=_is_paid(obj),
            )
        obj.payment_method = method
        # Gift-voucher / course-session redemptions involve no payment gateway.
        if method in (METHOD_GIFT_VOUCHER, METHOD_COURSE_SUBSCRIPTION) and has("payment_provider"):
            obj.payment_provider = None

    if has("payment_gateway"):
        gw = str(getattr(obj, "payment_gateway", "") or "").strip()
        if gw.lower() in {"", "other", "unknown", "none", "null"}:
            obj.payment_gateway = None


METHOD_BY_KEY = {_key(v): v for v in METHOD_VALUES}
METHOD_VALUES_KEYS = frozenset(METHOD_BY_KEY)


# ── branch denormalisation (for reports) ──────────────────────────────────────


def apply_branch_name(obj: Any) -> None:
    """Fill ``branch_name`` from ``branch_data`` so reports can group / label by branch."""
    if not hasattr(obj, "branch_name"):
        return
    if getattr(obj, "branch_name", None):
        return
    data = getattr(obj, "branch_data", None)
    if isinstance(data, dict):
        name = data.get("name") or data.get("branch_name")
        if name:
            obj.branch_name = str(name)[:255]
