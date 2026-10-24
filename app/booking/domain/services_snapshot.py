"""
app/booking/domain/services_snapshot.py
───────────────────────────────────────
Pure helpers for the multi-service booking model.

A booking stores an ordered list of *service entries* in ``bookings.services``
(JSONB).  Every entry is a snapshot taken at booking time::

    {
        "service_id": "uuid",
        "service_data": {...},
        "service_arrangement_id": "uuid" | None,
        "service_arrangement_data": {...},
        "base_price": "0.000",        # price of this service (Decimal as str)
        "duration_minutes": 60,       # base duration of this service
        "position": 0,
    }

All services of one booking share the same branch and therapist, which stay as
top-level booking columns.  The legacy single-service columns
(``service_id`` / ``service_data`` / ``service_arrangement_id`` /
``service_arrangement_data``) always mirror ``services[0]`` (the primary
service) so existing consumers keep working.

These helpers are side-effect free and tolerant of partially populated rows
(legacy rows, test doubles) – they never raise on malformed data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_SERVICES_PER_BOOKING = 10


def _to_str_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, uuid.UUID)):
        s = str(value).strip()
        return s or None
    return None


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_service_entry(
    *,
    service_id: Any,
    service_data: dict | None = None,
    service_arrangement_id: Any = None,
    service_arrangement_data: dict | None = None,
    base_price: Any = None,
    duration_minutes: Any = None,
    position: int = 0,
) -> dict[str, Any]:
    """Build one JSON-serialisable service entry (UUIDs / Decimals as strings)."""
    svc = dict(service_data or {})
    price = _to_decimal(base_price if base_price is not None else svc.get("base_price"))
    duration = _to_int(
        duration_minutes
        if duration_minutes is not None
        else svc.get("base_duration") or svc.get("duration_minutes"),
        0,
    )
    return {
        "service_id": _to_str_id(service_id) or "",
        "service_data": svc,
        "service_arrangement_id": _to_str_id(service_arrangement_id),
        "service_arrangement_data": dict(service_arrangement_data or {}),
        "base_price": f"{price:.3f}",
        "duration_minutes": duration,
        "position": position,
    }


def get_booking_services(booking: Any) -> list[dict[str, Any]]:
    """
    Return the booking's service entries.

    Uses ``booking.services`` when it is a non-empty list of dicts; otherwise
    synthesises a one-element list from the legacy single-service columns so
    rows created before the migration (and test doubles) still work.
    """
    raw = getattr(booking, "services", None)
    if isinstance(raw, list) and raw and all(isinstance(e, dict) for e in raw):
        return raw

    legacy_svc = getattr(booking, "service_data", None)
    legacy_arr = getattr(booking, "service_arrangement_data", None)
    legacy_id = _to_str_id(getattr(booking, "service_id", None))
    if legacy_id is None and not isinstance(legacy_svc, dict):
        return []
    return [
        build_service_entry(
            service_id=legacy_id,
            service_data=legacy_svc if isinstance(legacy_svc, dict) else {},
            service_arrangement_id=getattr(booking, "service_arrangement_id", None),
            service_arrangement_data=legacy_arr if isinstance(legacy_arr, dict) else {},
            base_price=getattr(booking, "base_price", None)
            if isinstance(getattr(booking, "base_price", None), (str, int, float, Decimal))
            else None,
            duration_minutes=getattr(booking, "duration_minutes", None)
            if isinstance(getattr(booking, "duration_minutes", None), int)
            else None,
            position=0,
        )
    ]


def entry_service_name(entry: dict[str, Any]) -> str:
    sd = entry.get("service_data") or {}
    return str(sd.get("name") or sd.get("service_name") or "")


def entry_arrangement_name(entry: dict[str, Any]) -> str:
    ad = entry.get("service_arrangement_data") or {}
    return str(ad.get("arrangement_name") or ad.get("room_name") or ad.get("name") or "")


def service_names(booking_or_entries: Any) -> list[str]:
    """Ordered, non-empty service names of a booking (or of a list of entries)."""
    entries = (
        booking_or_entries
        if isinstance(booking_or_entries, list)
        else get_booking_services(booking_or_entries)
    )
    return [n for n in (entry_service_name(e) for e in entries) if n]


def joined_service_name(booking_or_entries: Any, sep: str = ", ") -> str:
    """Display name for all services, e.g. ``"Massage, Facial"``."""
    return sep.join(service_names(booking_or_entries))


def total_services_price(entries: list[dict[str, Any]]) -> Decimal:
    return sum((_to_decimal(e.get("base_price")) for e in entries), Decimal("0"))


def total_services_duration(entries: list[dict[str, Any]]) -> int:
    return sum(_to_int(e.get("duration_minutes")) for e in entries)


def distinct_arrangement_ids(entries: list[dict[str, Any]]) -> list[str]:
    """Distinct non-empty arrangement ids, preserving order."""
    seen: dict[str, None] = {}
    for e in entries:
        aid = _to_str_id(e.get("service_arrangement_id"))
        if aid:
            seen.setdefault(aid, None)
    return list(seen)


def effective_points(entry: dict[str, Any]) -> tuple[int, int]:
    """
    ``(loyalty_points, price_in_points)`` of an entry; arrangement-level
    overrides take precedence over service-level values when present.
    """
    sd = entry.get("service_data") or {}
    ad = entry.get("service_arrangement_data") or {}
    arr_lp = ad.get("loyalty_points")
    arr_pp = ad.get("price_in_points")
    lp = _to_int(arr_lp) if arr_lp is not None else _to_int(sd.get("loyalty_points"))
    pp = _to_int(arr_pp) if arr_pp is not None else _to_int(sd.get("price_in_points"))
    return lp, pp


def primary_fields(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Legacy single-service column values mirrored from ``services[0]``."""
    first = entries[0]
    return {
        "service_id": uuid.UUID(first["service_id"]) if first.get("service_id") else None,
        "service_data": first.get("service_data") or {},
        "service_arrangement_id": (
            uuid.UUID(first["service_arrangement_id"])
            if first.get("service_arrangement_id")
            else None
        ),
        "service_arrangement_data": first.get("service_arrangement_data") or {},
    }


def points_cost(entry: dict[str, Any]) -> int:
    """Loyalty redemption cost of one entry (arrangement override, else service level)."""
    sd = entry.get("service_data") or {}
    ad = entry.get("service_arrangement_data") or {}
    return _to_int(ad.get("price_in_points")) or _to_int(sd.get("price_in_points"))


def cache_services_payload(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact per-service payload sent to ushauth's appointment cache."""
    return [
        {
            "service_id": e.get("service_id") or None,
            "service_name": entry_service_name(e) or None,
            "service_arrangement_id": e.get("service_arrangement_id") or None,
            "service_arrangement_name": entry_arrangement_name(e) or None,
            "duration_minutes": _to_int(e.get("duration_minutes")),
        }
        for e in entries
    ]
