"""
app/booking/application/multi_service.py
────────────────────────────────────────
Resolve the per-service snapshots of a (multi-service) booking request.

For every requested service the ushauth ``service`` and ``service arrangement``
records are fetched (source of truth) **concurrently**, deduplicated by id, and
merged with the client supplied snapshot.  The merge rules are identical to the
historic single-service logic in ``POST /bookings/`` so legacy payloads produce
byte-identical snapshots.

All services of a booking share the same branch and therapist; this module also
validates the cross-service invariants that can be checked from ushauth data.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.booking.domain.services_snapshot import build_service_entry
from app.core.logging import get_logger

logger = get_logger(__name__)

# Upper bound for parallel ushauth look-ups per request.
_FETCH_CONCURRENCY = 5


class ServiceValidationError(ValueError):
    """Raised when the requested services are inconsistent (maps to HTTP 422)."""


def _unwrap(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        return data if isinstance(data, dict) else {}
    return {}


async def _fetch_all(
    ushauth: Any,
    service_ids: list[str],
    arrangement_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch distinct services / arrangements concurrently. Failures are non-fatal."""
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(kind: str, key: str) -> tuple[str, str, dict[str, Any]]:
        async with sem:
            try:
                if kind == "service":
                    raw = await ushauth.get_service(key)
                else:
                    raw = await ushauth.get_service_arrangement(key)
                return kind, key, _unwrap(raw)
            except Exception as exc:  # noqa: BLE001 – upstream failures degrade gracefully
                if kind == "service":
                    logger.warning("fetch_service_details_failed", service_id=key, error=str(exc))
                else:
                    logger.warning("fetch_arrangement_details_failed", arrangement_id=key, error=str(exc))
                return kind, key, {}

    tasks = [_one("service", sid) for sid in service_ids] + [
        _one("arrangement", aid) for aid in arrangement_ids
    ]
    results = await asyncio.gather(*tasks)
    services = {k: d for kind, k, d in results if kind == "service"}
    arrangements = {k: d for kind, k, d in results if kind == "arrangement"}
    return services, arrangements


def _build_one(
    item: Any,
    position: int,
    db_service: dict[str, Any],
    db_arrangement: dict[str, Any],
    is_loyalty_booking: bool,
) -> dict[str, Any]:
    """Merge one request item with ushauth data into a stored service entry."""
    item_sd = item.service_data or {}
    item_ad = item.service_arrangement_data or {}

    # Arrangement-level overrides for *this* service
    db_arr_loyalty = None
    db_arr_price_pts = None
    if item.service_id and db_arrangement:
        for asvc in db_arrangement.get("arrangement_services", []) or []:
            if str(asvc.get("service_id")) == str(item.service_id):
                db_arr_loyalty = asvc.get("loyalty_points")
                db_arr_price_pts = asvc.get("price_in_points")
                break

    db_loyalty = db_service.get("loyalty_points")
    db_price_pts = db_service.get("price_in_points")
    db_eligible = db_service.get("is_eligible_for_loyalty")

    if db_loyalty is not None and int(db_loyalty) > 0:
        loyalty_points = int(db_loyalty)
    elif item.loyalty_points and int(item.loyalty_points) > 0:
        loyalty_points = int(item.loyalty_points)
    else:
        loyalty_points = int(item_sd.get("loyalty_points", 0) or 0)

    if db_price_pts is not None and int(db_price_pts) > 0:
        price_in_points = int(db_price_pts)
    elif item.price_in_points and int(item.price_in_points) > 0:
        price_in_points = int(item.price_in_points)
    else:
        price_in_points = int(item_sd.get("price_in_points", 0) or 0)

    if db_eligible is not None:
        is_eligible = bool(db_eligible)
    else:
        is_eligible = bool(item.is_eligible_for_loyalty or item_sd.get("is_eligible_for_loyalty", False))

    if db_arr_loyalty is not None and int(db_arr_loyalty) > 0:
        arr_loyalty_points: Any = int(db_arr_loyalty)
    elif item.arrangement_loyalty_points is not None:
        arr_loyalty_points = int(item.arrangement_loyalty_points)
    else:
        arr_loyalty_points = item_ad.get("loyalty_points")

    if db_arr_price_pts is not None and int(db_arr_price_pts) > 0:
        arr_price_in_points: Any = int(db_arr_price_pts)
    elif item.arrangement_price_in_points is not None:
        arr_price_in_points = int(item.arrangement_price_in_points)
    else:
        arr_price_in_points = item_ad.get("price_in_points")

    if is_loyalty_booking:
        # Redeeming points to buy a service must never earn loyalty points
        loyalty_points = 0
        arr_loyalty_points = 0
        is_eligible = False

    service_id = item.service_id or uuid.uuid4()

    service_name = (
        item.service_name
        or db_service.get("name")
        or (item_sd.get("service_name") or item_sd.get("name") or "")
    )
    service_category = (
        item.service_category
        or db_service.get("category")
        or (item_sd.get("service_category") or item_sd.get("category") or "")
    )
    base_price = str(
        item.base_price
        or db_service.get("base_price")
        or item_sd.get("base_price")
        or "0"
    )
    base_duration = int(
        item.base_duration
        or db_service.get("duration_minutes")
        or item_sd.get("base_duration")
        or item_sd.get("duration")
        or 60
    )
    service_data = {
        "id": str(item.service_id) if item.service_id else "",
        "name": service_name,
        "category": service_category,
        "base_price": base_price,
        "base_duration": base_duration,
        **item_sd,
        "is_eligible_for_loyalty": is_eligible,
        "loyalty_points": loyalty_points,
        "price_in_points": price_in_points,
    }

    arrangement_type = (
        item.arrangement_type
        or db_arrangement.get("arrangement_type")
        or (item_ad.get("arrangement_type") or item_ad.get("type") or "")
    )
    room_name = (
        item.room_name
        or item.arrangement_name
        or db_arrangement.get("name")
        or (item_ad.get("arrangement_name") or item_ad.get("room_name") or item_ad.get("name") or "")
    )
    arrangement_data = {
        "id": str(item.service_arrangement_id) if item.service_arrangement_id else "",
        "arrangement_type": arrangement_type,
        "room_name": room_name,
        "arrangement_name": room_name,
        **item_ad,
        "loyalty_points": arr_loyalty_points,
        "price_in_points": arr_price_in_points,
    }

    return build_service_entry(
        service_id=service_id,
        service_data=service_data,
        service_arrangement_id=item.service_arrangement_id,
        service_arrangement_data=arrangement_data,
        base_price=base_price,
        duration_minutes=base_duration,
        position=position,
    )


async def resolve_service_entries(
    ushauth: Any,
    items: list[Any],
    *,
    is_loyalty_booking: bool = False,
) -> list[dict[str, Any]]:
    """
    Build the ordered list of stored service entries for a booking request.

    Raises :class:`ServiceValidationError` when the services are inconsistent:
    a service is not offered by its arrangement, or the arrangements belong to
    different branches.
    """
    service_ids = list(dict.fromkeys(str(i.service_id) for i in items if i.service_id))
    arrangement_ids = list(
        dict.fromkeys(str(i.service_arrangement_id) for i in items if i.service_arrangement_id)
    )
    db_services, db_arrangements = await _fetch_all(ushauth, service_ids, arrangement_ids)

    entries: list[dict[str, Any]] = []
    for pos, item in enumerate(items):
        db_service = db_services.get(str(item.service_id), {}) if item.service_id else {}
        db_arr = (
            db_arrangements.get(str(item.service_arrangement_id), {})
            if item.service_arrangement_id
            else {}
        )
        entries.append(_build_one(item, pos, db_service, db_arr, is_loyalty_booking))

    if len(items) > 1:
        _validate_consistency(items, db_arrangements)
    return entries


def _validate_consistency(items: list[Any], db_arrangements: dict[str, dict[str, Any]]) -> None:
    """Cross-service checks that can be derived from ushauth data."""
    # 1. Each service must be offered by the arrangement it is booked in.
    for idx, item in enumerate(items):
        if not (item.service_id and item.service_arrangement_id):
            continue
        arr = db_arrangements.get(str(item.service_arrangement_id)) or {}
        offered = arr.get("arrangement_services")
        if not offered:
            continue  # arrangement payload without the relation (or fetch failed) – skip
        active_ids = {
            str(a.get("service_id"))
            for a in offered
            if a.get("is_active", True)
        }
        if str(item.service_id) not in active_ids:
            raise ServiceValidationError(
                f"services[{idx}]: service is not available in the selected arrangement."
            )

    # 2. All arrangements must belong to the same branch.
    branch_names = {
        str(arr.get("branch_name")).strip().lower()
        for arr in db_arrangements.values()
        if arr.get("branch_name")
    }
    if len(branch_names) > 1:
        raise ServiceValidationError("All services in a booking must belong to the same branch.")
