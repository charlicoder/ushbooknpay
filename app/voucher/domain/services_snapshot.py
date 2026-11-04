"""
app/voucher/domain/services_snapshot.py
───────────────────────────────────────
Multi-service support for gift vouchers.

A voucher may gift several services. Each entry of ``GiftVoucher.services`` is::

    {
        "service_id": "<uuid>",
        "service_data": {...},          # snapshot (name, duration_minutes, base_price, ...)
        "addons": [{"addon_id", "name", "price", "duration_minutes"}],
        "extra_time": 15,               # minutes, for THIS service
        "price_for_extra_time": "5.000",
        "position": 0,
    }

Backward compatibility
----------------------
The legacy single-service columns (``service_id``, ``service_data``, ``addons``,
``extra_time``, ``price_for_extra_time``) are still written and mirror the voucher:

* ``service_id`` / ``service_data`` → primary (first) service
* ``addons``                        → add-ons of ALL services (flattened)
* ``extra_time``                    → sum of the extra time of all services
* ``price_for_extra_time``          → primary service's rate

Vouchers created before this feature have no ``services`` value; they are read as a
single-entry list built from the legacy columns, so every consumer can rely on
:func:`voucher_services_of`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

MAX_VOUCHER_SERVICES = 10


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _addon_duration(addon: dict[str, Any]) -> int:
    return _to_int(addon.get("duration_minutes")) or _to_int(addon.get("duration"))


def normalise_addon(addon: Any) -> dict[str, Any] | None:
    if not isinstance(addon, dict):
        return None
    out = dict(addon)
    aid = out.get("addon_id") or out.get("id")
    if aid is not None:
        out["addon_id"] = str(aid)
    out["duration_minutes"] = _addon_duration(out)
    return out


def _service_id_str(entry: dict[str, Any]) -> str | None:
    sid = entry.get("service_id") or (entry.get("service_data") or {}).get("id")
    if sid in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(sid)))
    except ValueError:
        return str(sid)


def normalise_service_entry(raw: Any, position: int) -> dict[str, Any] | None:
    """Normalise one service entry (accepts either full entries or bare service snapshots)."""
    if not isinstance(raw, dict):
        return None
    data = raw.get("service_data") if isinstance(raw.get("service_data"), dict) else None
    if data is None:
        # bare service snapshot (e.g. {"id", "name", "duration_minutes", ...})
        data = {k: v for k, v in raw.items() if k not in ("addons", "extra_time", "price_for_extra_time", "position", "service_id")}
        if raw.get("service_id") and not data.get("id"):
            data["id"] = raw["service_id"]
    entry: dict[str, Any] = {
        "service_id": None,
        "service_data": dict(data or {}),
        "addons": [a for a in (normalise_addon(x) for x in (raw.get("addons") or [])) if a],
        "extra_time": max(_to_int(raw.get("extra_time")), 0),
        "price_for_extra_time": (
            str(raw["price_for_extra_time"]) if raw.get("price_for_extra_time") not in (None, "") else None
        ),
        "position": position,
    }
    entry["service_id"] = _service_id_str({**raw, "service_data": entry["service_data"]})
    if entry["service_id"] is None and not entry["service_data"]:
        return None
    return entry


def normalise_services(
    services: list[Any] | None,
    *,
    service_id: Any = None,
    service_data: dict[str, Any] | None = None,
    addons: list[dict[str, Any]] | None = None,
    extra_time: int | None = 0,
    price_for_extra_time: Any = None,
) -> list[dict[str, Any]]:
    """
    Return the canonical services list.

    When ``services`` is empty the legacy single-service arguments are wrapped into a
    one-entry list (old clients keep working unchanged).
    """
    entries: list[dict[str, Any]] = []
    for raw in services or []:
        entry = normalise_service_entry(raw, len(entries))
        if entry:
            entries.append(entry)

    if not entries and (service_id or service_data):
        legacy = normalise_service_entry(
            {
                "service_id": str(service_id) if service_id else None,
                "service_data": service_data or {},
                "addons": addons or [],
                "extra_time": extra_time or 0,
                "price_for_extra_time": price_for_extra_time,
            },
            0,
        )
        if legacy:
            entries.append(legacy)
    return entries[:MAX_VOUCHER_SERVICES]


def mirror_legacy_fields(services: list[dict[str, Any]]) -> dict[str, Any]:
    """Legacy single-service column values derived from the services list."""
    if not services:
        return {}
    primary = services[0]
    flat_addons: list[dict[str, Any]] = []
    for s in services:
        flat_addons.extend(s.get("addons") or [])
    price = primary.get("price_for_extra_time")
    return {
        "service_id": uuid.UUID(primary["service_id"]) if _is_uuid(primary.get("service_id")) else None,
        "service_data": primary.get("service_data") or {},
        "addons": flat_addons,
        "extra_time": sum(_to_int(s.get("extra_time")) for s in services),
        "price_for_extra_time": Decimal(str(price)) if price not in (None, "") else None,
    }


def services_duration(services: list[dict[str, Any]]) -> int:
    """Total minutes: services + add-ons + extra time."""
    total = 0
    for s in services:
        sd = s.get("service_data") or {}
        total += _to_int(sd.get("duration_minutes")) or _to_int(sd.get("duration"))
        total += sum(_addon_duration(a) for a in (s.get("addons") or []) if isinstance(a, dict))
        total += _to_int(s.get("extra_time"))
    return total


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def voucher_services_of(voucher: Any) -> list[dict[str, Any]]:
    """Services of a voucher; legacy single-service vouchers are returned as one entry."""
    stored = getattr(voucher, "services", None)
    if isinstance(stored, list) and stored:
        return stored
    legacy_id = getattr(voucher, "service_id", None)
    legacy_data = getattr(voucher, "service_data", None) or {}
    if not legacy_id and not legacy_data:
        return []
    return normalise_services(
        None,
        service_id=legacy_id,
        service_data=legacy_data,
        addons=getattr(voucher, "addons", None) or [],
        extra_time=getattr(voucher, "extra_time", 0) or 0,
        price_for_extra_time=getattr(voucher, "price_for_extra_time", None),
    )



# ── Partial redemption ─────────────────────────────────────────────────────────
# GiftVoucher.redemptions: one record per booking created from the voucher:
# {"booking_id", "booking_number", "redeemed_at", "redeemed_by",
#  "services": [{"service_id", "service_name", "addon_ids": [...], "extra_time": int}]}


def voucher_redemptions_of(voucher: Any) -> list[dict[str, Any]]:
    stored = getattr(voucher, "redemptions", None)
    return [r for r in stored if isinstance(r, dict)] if isinstance(stored, list) else []


def used_service_ids(voucher: Any) -> set[str]:
    """Service ids already consumed by earlier (partial) redemptions."""
    used: set[str] = set()
    for r in voucher_redemptions_of(voucher):
        for s in r.get("services") or []:
            if isinstance(s, dict) and s.get("service_id"):
                used.add(str(s["service_id"]))
    return used


def remaining_service_ids(voucher: Any) -> list[str]:
    """Gifted services not redeemed yet (all of them for a never-redeemed voucher)."""
    used = used_service_ids(voucher)
    return [
        str(e["service_id"])
        for e in voucher_services_of(voucher)
        if e.get("service_id") and str(e["service_id"]) not in used
    ]
