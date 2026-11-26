"""
app/common/discount_utils.py
────────────────────────────
Shared proportional discount validation and allocation utilities for bookings
and gift vouchers.

Accounting rules:
1. Discount must be >= 0 and strictly less than the pre-discount subtotal
   (0 < discount < subtotal whenever a discount is applied).
2. Applying a discount requires a valid requester name selected from the
   configured DISCOUNT_REQUESTED_BY environment list.
3. The discount is allocated proportionally across every positive line item
   (services, add-ons, extra time) with deterministic 0.001 KWD penny
   reconciliation on the largest item so:
       sum(item.discount_amount) == discount
       sum(item.discounted_price) == subtotal - discount == total_amount
4. Original pre-discount prices are always preserved alongside the
   proportionally discounted prices for finance & accounting auditability.
"""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.core.exceptions import ValidationError

_ZERO = Decimal("0.000")
_QUANT = Decimal("0.001")


def to_decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return default


def parse_discount_requesters(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def validate_discount(
    *,
    subtotal: Decimal,
    discount: Decimal,
    discount_requested_by: str | None,
    allowed_requesters: list[str] | None = None,
) -> tuple[Decimal, str | None]:
    """
    Validate discount amount and requester name.

    Returns ``(quantized_discount, canonical_requester_name)``.
    Raises ``ValidationError`` if invalid.
    """
    sub = to_decimal(subtotal)
    disc = to_decimal(discount)

    if disc < _ZERO:
        raise ValidationError("Discount amount cannot be negative.")

    if disc == _ZERO:
        return _ZERO, None

    if sub <= _ZERO or disc >= sub:
        raise ValidationError(
            f"Discount amount ({disc:.3f}) must be less than the total payment amount ({sub:.3f})."
        )

    requester = (discount_requested_by or "").strip()
    if not requester:
        raise ValidationError(
            "Discount cannot be applied without selecting a name from 'Discount Requested By'."
        )

    if allowed_requesters:
        canonical_map = {name.lower(): name for name in allowed_requesters}
        if requester.lower() not in canonical_map:
            raise ValidationError(
                f"'{requester}' is not in the authorized Discount Requested By list."
            )
        requester = canonical_map[requester.lower()]

    return disc, requester


def allocate_proportional_amounts(
    amounts: list[Decimal],
    discount: Decimal,
) -> list[tuple[Decimal, Decimal]]:
    """
    Proportionally allocate ``discount`` across ``amounts``.

    Returns a list of ``(discounted_price, item_discount)`` tuples of the same
    length as ``amounts``, guaranteed to satisfy:
      - ``sum(item_discount) == discount``
      - ``sum(discounted_price) == sum(amounts) - discount``
    """
    quant_amounts = [max(_ZERO, to_decimal(a)) for a in amounts]
    disc = max(_ZERO, to_decimal(discount))
    total = sum(quant_amounts, _ZERO)

    if disc <= _ZERO or total <= _ZERO:
        return [(a, _ZERO) for a in quant_amounts]

    if disc >= total:
        # Defensive clamp (caller should have already validated disc < total)
        disc = max(_ZERO, total - _QUANT)

    item_discounts: list[Decimal] = []
    for amt in quant_amounts:
        if amt <= _ZERO:
            item_discounts.append(_ZERO)
        else:
            share = (amt * disc / total).quantize(_QUANT, rounding=ROUND_HALF_UP)
            share = min(share, amt)
            item_discounts.append(share)

    # Reconcile any 0.001 KWD rounding residue on the largest positive item
    diff = disc - sum(item_discounts, _ZERO)
    if diff != _ZERO:
        positive_indices = [i for i, a in enumerate(quant_amounts) if a > _ZERO]
        if positive_indices:
            largest_idx = max(positive_indices, key=lambda i: quant_amounts[i])
            adjusted = item_discounts[largest_idx] + diff
            if _ZERO <= adjusted <= quant_amounts[largest_idx]:
                item_discounts[largest_idx] = adjusted.quantize(_QUANT, rounding=ROUND_HALF_UP)

    result: list[tuple[Decimal, Decimal]] = []
    for amt, d_amt in zip(quant_amounts, item_discounts):
        net = max(_ZERO, (amt - d_amt).quantize(_QUANT, rounding=ROUND_HALF_UP))
        result.append((net, d_amt))
    return result


def apply_proportional_discount_to_booking(
    *,
    service_entries: list[dict[str, Any]],
    addons: list[dict[str, Any]] | None,
    sessions: list[dict[str, Any]] | None,
    extra_price: Decimal,
    discount: Decimal,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Decimal]:
    """
    Apply ``discount`` proportionally across booking services, addons, and extra time.

    Preserves ``original_base_price`` / ``original_price`` and ``discount_amount`` on
    every item while updating ``base_price`` / ``price`` to the discounted values.
    Also updates session-level snapshots if ``sessions`` are present.

    Returns ``(updated_services, updated_addons, updated_sessions, discounted_extra_price)``.
    """
    svcs = copy.deepcopy(service_entries or [])
    adds = copy.deepcopy(addons or [])
    sess_list = copy.deepcopy(sessions or [])
    ext = max(_ZERO, to_decimal(extra_price))
    disc = max(_ZERO, to_decimal(discount))

    if disc <= _ZERO:
        return svcs, adds, sess_list, ext

    # Collect all positive price slots: services, then addons, then extra_price
    amounts: list[Decimal] = []
    for s in svcs:
        orig = to_decimal(
            s.get("original_base_price")
            if s.get("original_base_price") is not None
            else (s.get("base_price") or (s.get("service_data") or {}).get("base_price") or (s.get("service_data") or {}).get("price"))
        )
        amounts.append(orig)

    for a in adds:
        orig = to_decimal(
            a.get("original_price")
            if a.get("original_price") is not None
            else (a.get("price") or a.get("addon_price"))
        )
        amounts.append(orig)

    has_extra = ext > _ZERO
    if has_extra:
        amounts.append(ext)

    allocated = allocate_proportional_amounts(amounts, disc)
    idx = 0

    for s in svcs:
        orig = amounts[idx]
        net, d_amt = allocated[idx]
        idx += 1
        s["original_base_price"] = f"{orig:.3f}"
        s["original_price"] = f"{orig:.3f}"
        s["discount_amount"] = f"{d_amt:.3f}"
        s["discounted_price"] = f"{net:.3f}"
        s["base_price"] = f"{net:.3f}"
        s["price"] = f"{net:.3f}"
        if isinstance(s.get("service_data"), dict):
            s["service_data"]["original_price"] = f"{orig:.3f}"
            s["service_data"]["discount_amount"] = f"{d_amt:.3f}"
            s["service_data"]["discounted_price"] = f"{net:.3f}"
            s["service_data"]["base_price"] = f"{net:.3f}"
            s["service_data"]["price"] = f"{net:.3f}"

    for a in adds:
        orig = amounts[idx]
        net, d_amt = allocated[idx]
        idx += 1
        a["original_price"] = f"{orig:.3f}"
        a["discount_amount"] = f"{d_amt:.3f}"
        a["discounted_price"] = f"{net:.3f}"
        a["price"] = f"{net:.3f}"

    discounted_extra = ext
    if has_extra:
        discounted_extra, _ = allocated[idx]

    # Mirror proportional discount into multi-session snapshots (if any)
    if sess_list:
        sess_amounts: list[Decimal] = []
        sess_slots: list[tuple[str, int, int]] = []  # (kind, sess_idx, sub_idx)
        for si, sess in enumerate(sess_list):
            if not isinstance(sess, dict):
                continue
            s_svcs = sess.get("services")
            if isinstance(s_svcs, list) and s_svcs:
                for sub_i, sub_s in enumerate(s_svcs):
                    if isinstance(sub_s, dict):
                        p = to_decimal(
                            sub_s.get("original_base_price")
                            if sub_s.get("original_base_price") is not None
                            else (sub_s.get("base_price") or sub_s.get("price"))
                        )
                        sess_amounts.append(p)
                        sess_slots.append(("svc", si, sub_i))
            else:
                p = to_decimal(
                    sess.get("original_service_price")
                    if sess.get("original_service_price") is not None
                    else (sess.get("service_price") or sess.get("base_price"))
                )
                if p > _ZERO:
                    sess_amounts.append(p)
                    sess_slots.append(("sess_svc", si, 0))

            s_adds = sess.get("addons")
            if isinstance(s_adds, list):
                for ai, add in enumerate(s_adds):
                    if isinstance(add, dict):
                        p = to_decimal(
                            add.get("original_price")
                            if add.get("original_price") is not None
                            else add.get("price")
                        )
                        if p > _ZERO:
                            sess_amounts.append(p)
                            sess_slots.append(("addon", si, ai))

            s_ext = to_decimal(
                sess.get("original_extra_price")
                if sess.get("original_extra_price") is not None
                else (sess.get("extra_price") or sess.get("price_for_extra_minutes"))
            )
            if s_ext > _ZERO:
                sess_amounts.append(s_ext)
                sess_slots.append(("extra", si, 0))

        if sess_amounts and sum(sess_amounts, _ZERO) > _ZERO:
            sess_alloc = allocate_proportional_amounts(sess_amounts, disc)
            for (kind, si, sub_i), orig, (net, d_amt) in zip(sess_slots, sess_amounts, sess_alloc):
                sess = sess_list[si]
                if kind == "svc":
                    sub_s = sess["services"][sub_i]
                    sub_s["original_base_price"] = f"{orig:.3f}"
                    sub_s["original_price"] = f"{orig:.3f}"
                    sub_s["discount_amount"] = f"{d_amt:.3f}"
                    sub_s["discounted_price"] = f"{net:.3f}"
                    sub_s["base_price"] = f"{net:.3f}"
                    sub_s["price"] = f"{net:.3f}"
                elif kind == "sess_svc":
                    sess["original_service_price"] = f"{orig:.3f}"
                    sess["service_price"] = f"{net:.3f}"
                    sess["base_price"] = f"{net:.3f}"
                elif kind == "addon":
                    add = sess["addons"][sub_i]
                    add["original_price"] = f"{orig:.3f}"
                    add["discount_amount"] = f"{d_amt:.3f}"
                    add["discounted_price"] = f"{net:.3f}"
                    add["price"] = f"{net:.3f}"
                elif kind == "extra":
                    sess["original_extra_price"] = f"{orig:.3f}"
                    sess["extra_price"] = f"{net:.3f}"
                    sess["price_for_extra_minutes"] = f"{net:.3f}"

            # Recompute per-session rollups
            for sess in sess_list:
                if not isinstance(sess, dict):
                    continue
                if isinstance(sess.get("services"), list) and sess["services"]:
                    svc_sum = sum(
                        (to_decimal(x.get("base_price")) for x in sess["services"] if isinstance(x, dict)),
                        _ZERO,
                    )
                    sess["service_price"] = f"{svc_sum:.3f}"
                add_sum = sum(
                    (to_decimal(x.get("price")) for x in (sess.get("addons") or []) if isinstance(x, dict)),
                    _ZERO,
                )
                ext_sum = to_decimal(sess.get("extra_price") or sess.get("price_for_extra_minutes"))
                svc_val = to_decimal(sess.get("service_price") or sess.get("base_price"))
                sess["addons_price"] = f"{add_sum:.3f}"
                sess["total_price"] = f"{(svc_val + add_sum + ext_sum):.3f}"

    return svcs, adds, sess_list, discounted_extra


def apply_proportional_discount_to_voucher(
    *,
    service_entries: list[dict[str, Any]],
    addons: list[dict[str, Any]] | None,
    discount: Decimal,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Apply ``discount`` proportionally across gift voucher service entries, their
    nested addons, and any extra-time charges.

    Preserves ``original_base_price`` / ``original_price`` and ``discount_amount``
    while updating ``base_price`` / ``price`` to the proportionally discounted values.
    """
    svcs = copy.deepcopy(service_entries or [])
    adds = copy.deepcopy(addons or [])
    disc = max(_ZERO, to_decimal(discount))

    if disc <= _ZERO:
        return svcs, adds

    amounts: list[Decimal] = []
    slots: list[tuple[str, int, int]] = []  # (kind, svc_idx, sub_idx)

    has_nested_addons = any(bool(s.get("addons")) for s in svcs if isinstance(s, dict))

    for si, s in enumerate(svcs):
        if not isinstance(s, dict):
            continue
        sd = s.get("service_data") or {}
        orig_svc = to_decimal(
            s.get("original_base_price")
            if s.get("original_base_price") is not None
            else (
                s.get("base_price")
                or s.get("price")
                or sd.get("base_price")
                or sd.get("price")
                or (s.get("service_arrangement_data") or {}).get("effective_price")
            )
        )
        amounts.append(orig_svc)
        slots.append(("svc", si, 0))

        for ai, a in enumerate(s.get("addons") or []):
            if isinstance(a, dict):
                orig_a = to_decimal(
                    a.get("original_price")
                    if a.get("original_price") is not None
                    else a.get("price")
                )
                amounts.append(orig_a)
                slots.append(("nested_addon", si, ai))

        ext_mins = int(float(s.get("extra_time") or 0))
        ext_unit = to_decimal(s.get("original_price_for_extra_time") or s.get("price_for_extra_time"))
        if ext_mins > 0 and ext_unit > _ZERO:
            amounts.append(ext_unit)
            slots.append(("extra", si, 0))

    if not has_nested_addons:
        for ai, a in enumerate(adds):
            if isinstance(a, dict):
                orig_a = to_decimal(
                    a.get("original_price")
                    if a.get("original_price") is not None
                    else a.get("price")
                )
                amounts.append(orig_a)
                slots.append(("flat_addon", ai, 0))

    if not amounts or sum(amounts, _ZERO) <= _ZERO:
        return svcs, adds

    allocated = allocate_proportional_amounts(amounts, disc)

    for (kind, idx1, idx2), orig, (net, d_amt) in zip(slots, amounts, allocated):
        if kind == "svc":
            s = svcs[idx1]
            s["original_base_price"] = f"{orig:.3f}"
            s["original_price"] = f"{orig:.3f}"
            s["discount_amount"] = f"{d_amt:.3f}"
            s["discounted_price"] = f"{net:.3f}"
            s["base_price"] = f"{net:.3f}"
            s["price"] = f"{net:.3f}"
            if isinstance(s.get("service_data"), dict):
                s["service_data"]["original_price"] = f"{orig:.3f}"
                s["service_data"]["discount_amount"] = f"{d_amt:.3f}"
                s["service_data"]["discounted_price"] = f"{net:.3f}"
                s["service_data"]["base_price"] = f"{net:.3f}"
                s["service_data"]["price"] = f"{net:.3f}"
        elif kind == "nested_addon":
            a = svcs[idx1]["addons"][idx2]
            a["original_price"] = f"{orig:.3f}"
            a["discount_amount"] = f"{d_amt:.3f}"
            a["discounted_price"] = f"{net:.3f}"
            a["price"] = f"{net:.3f}"
        elif kind == "extra":
            s = svcs[idx1]
            s["original_price_for_extra_time"] = f"{orig:.3f}"
            s["extra_time_discount_amount"] = f"{d_amt:.3f}"
            s["price_for_extra_time"] = f"{net:.3f}"
        elif kind == "flat_addon":
            a = adds[idx1]
            a["original_price"] = f"{orig:.3f}"
            a["discount_amount"] = f"{d_amt:.3f}"
            a["discounted_price"] = f"{net:.3f}"
            a["price"] = f"{net:.3f}"

    # Rebuild flattened addons from nested addons when nested addons exist
    if has_nested_addons:
        flat: list[dict[str, Any]] = []
        for s in svcs:
            if isinstance(s, dict):
                flat.extend(copy.deepcopy(s.get("addons") or []))
        adds = flat

    return svcs, adds
