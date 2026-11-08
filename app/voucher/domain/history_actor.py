"""
app/voucher/domain/history_actor.py
───────────────────────────────────
Resolve WHO performed an entry of a gift voucher's ``update_history``.

Vouchers sold on USHWeb / USHSpa are created and settled by the customer through the payment
gateway: there is no staff user. System-ish actor labels ("Payment Gateway", "Online", "system",
a bare "Staff" …) must therefore be shown as the customer (the sender). A real person's name
(e.g. a desk employee who later edited the voucher) is always kept as-is.

Channels are compared through ``normalise_through`` (case-insensitive, aliases) so legacy values
such as ``USHWEB`` / ``desk`` and canonical ``ushweb`` / ``ushdesk`` behave identically.
"""

from __future__ import annotations

from typing import Any

from app.common.payment_attribution import THROUGH_USHSPA, THROUGH_USHWEB, normalise_through

ROLE_CUSTOMER = "Customer"
ROLE_STAFF = "Staff"

_GENERIC_NAMES = frozenset(
    {"", "staff", "system", "online", "gateway", "customer", "webhook", "scheduler", "unknown"}
)


def is_customer_channel(payment_through: Any) -> bool:
    """True when the voucher was sold through USHWeb or USHSpa (customer self-service)."""
    return normalise_through(payment_through) in (THROUGH_USHWEB, THROUGH_USHSPA)


def _is_generic_name(name: Any) -> bool:
    n = str(name or "").strip().lower()
    return n in _GENERIC_NAMES or n.startswith("payment gateway") or n.startswith("webhook")


def sender_display_name(sender_data: dict[str, Any] | None, fallback: str | None = None) -> str:
    sd = sender_data or {}
    return str(sd.get("name") or sd.get("full_name") or fallback or ROLE_CUSTOMER).strip() or ROLE_CUSTOMER


def resolve_history_actor(
    *,
    payment_through: Any,
    sender_data: dict[str, Any] | None,
    name: str | None,
    role: str | None,
    gift_from: str | None = None,
) -> tuple[str, str]:
    """Return the ``(name, role)`` to record / display for a history entry."""
    name_s = str(name or "").strip()
    role_s = str(role or "").strip()

    if is_customer_channel(payment_through):
        if _is_generic_name(name_s):
            return sender_display_name(sender_data, gift_from), ROLE_CUSTOMER
        # A real, named actor: keep the name. Without an explicit role, the sender is the
        # customer; anybody else is staff.
        if not role_s:
            role_s = ROLE_CUSTOMER if name_s == sender_display_name(sender_data) else ROLE_STAFF
        return name_s, role_s

    # Desk / unknown channel: staff acted.
    return name_s or ROLE_STAFF, role_s or ROLE_STAFF


def sanitize_history(
    history: list[dict[str, Any]] | None,
    *,
    payment_through: Any,
    sender_data: dict[str, Any] | None,
    gift_from: str | None = None,
) -> list[dict[str, Any]]:
    """Display-time correction of already-stored entries (copies; the stored JSON is untouched)."""
    out: list[dict[str, Any]] = []
    customer = is_customer_channel(payment_through)
    for entry in history or []:
        if not isinstance(entry, dict) or not customer:
            out.append(entry)
            continue
        current = (
            entry.get("performed_by_name")
            or entry.get("changed_by_name")
            or (entry.get("user_data") or {}).get("name")
        )
        if not _is_generic_name(current):
            out.append(entry)
            continue
        name, role = resolve_history_actor(
            payment_through=payment_through,
            sender_data=sender_data,
            name=current,
            role=None,
            gift_from=gift_from,
        )
        fixed = dict(entry)
        fixed["changed_by_name"] = name
        fixed["changed_by_role"] = role
        fixed["performed_by_name"] = name
        fixed["performed_by_role"] = role
        ud = dict(entry.get("user_data") or {})
        ud.update({"name": name, "role": role, "user_type": role.lower()})
        fixed["user_data"] = ud
        out.append(fixed)
    return out
