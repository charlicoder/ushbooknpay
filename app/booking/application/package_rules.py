"""
app/booking/application/package_rules.py
────────────────────────────────────────
Rules for booking a *packaged service* by its component services.

A client books a package by sending the package's **included services** across
``sessions[]`` (all in one session, one per session, or any custom grouping), each
service item tagged with ``package_id``.  This module

1. validates that every included service of the package is booked exactly once,
2. prices the package once (its own ``base_price``; ushauth already returns the sum of the
   components when the package price is 0) – the first component (session order) carries
   the package price, the others are recorded at 0.000,
3. stamps ``package_id`` / ``package_name`` on every component entry for reports.

Bookings that send the package itself as a normal service (legacy behaviour) never use
``package_id`` and are therefore untouched.
"""

from __future__ import annotations

from typing import Any

from app.booking.domain.services_snapshot import _to_decimal
from app.booking.domain.sessions import SessionValidationError


def _unwrap(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        return data if isinstance(data, dict) else {}
    return {}


async def resolve_package_context(
    ushauth: Any, items: list[Any], *, allow_component_swap: bool = False
) -> dict[str, dict[str, Any]]:
    """Validate package coverage across all session items; return per-package pricing context."""
    wanted: dict[str, list[tuple[int, int, str]]] = {}
    for si, item in enumerate(items):
        for sj, svc in enumerate(getattr(item, "services", None) or []):
            pid = getattr(svc, "package_id", None)
            if pid and svc.service_id:
                wanted.setdefault(str(pid), []).append((si, sj, str(svc.service_id)))
    if not wanted:
        return {}

    ctx: dict[str, dict[str, Any]] = {}
    for pid, refs in wanted.items():
        try:
            pkg = _unwrap(await ushauth.get_service(pid))
        except Exception as exc:  # noqa: BLE001
            raise SessionValidationError(f"Package {pid} could not be loaded.") from exc
        if not pkg.get("is_packaged_services"):
            raise SessionValidationError(f"Service {pid} is not a package.")
        included = pkg.get("included_services") or []
        if not included:
            raise SessionValidationError(f"Package '{pkg.get('name', pid)}' has no included services.")
        included_ids = sorted(str(c.get("id")) for c in included)
        requested_ids = sorted(r[2] for r in refs)
        if allow_component_swap:
            if len(requested_ids) != len(included_ids) or len(set(requested_ids)) != len(requested_ids):
                raise SessionValidationError(
                    f"Package '{pkg.get('name', pid)}' must be booked with each of its included "
                    "services exactly once."
                )
        elif requested_ids != included_ids:
            raise SessionValidationError(
                f"Package '{pkg.get('name', pid)}' must be booked with each of its included "
                "services exactly once."
            )
        carrier = min((r[0], r[1]) for r in refs)
        ctx[pid] = {
            "name": pkg.get("name") or "",
            "price": _to_decimal(pkg.get("base_price")),
            "carrier": carrier,
            "durations": {str(c.get("id")): c.get("duration_minutes") for c in included},
        }
    return ctx


def apply_package_context(
    entries: list[dict[str, Any]],
    svc_items: list[Any],
    session_index: int,
    ctx: dict[str, dict[str, Any]],
) -> None:
    """Stamp package info / pricing on the entries of one session (in place)."""
    if not ctx:
        return
    for j, (svc, entry) in enumerate(zip(svc_items, entries)):
        pid = str(getattr(svc, "package_id", "") or "")
        info = ctx.get(pid)
        if not info:
            continue
        sid = str(entry.get("service_id") or "")
        entry["package_id"] = pid
        entry["package_name"] = info["name"]
        entry["package_price"] = f"{info['price']:.3f}"
        entry["component_price"] = entry.get("base_price")
        duration = info["durations"].get(sid)
        if duration:
            entry["duration_minutes"] = int(duration)
        entry["base_price"] = f"{info['price']:.3f}" if info["carrier"] == (session_index, j) else "0.000"
        sd = entry.get("service_data")
        if isinstance(sd, dict):
            sd["package_id"] = pid
            sd["package_name"] = info["name"]


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def validate_package_exclusivity(sessions: list[dict[str, Any]]) -> None:
    """
    A package is booked on its own: never with another package or any other service.

    Entries are either components (``package_id`` set), a whole package (service_data flagged
    ``is_packaged_services``) or plain services.
    """
    package_ids: set[str] = set()
    whole_packages = 0
    plain = 0
    for sess in sessions:
        for entry in sess.get("services") or []:
            pid = entry.get("package_id")
            sd = entry.get("service_data") or {}
            if pid:
                package_ids.add(str(pid))
            elif _truthy(sd.get("is_packaged_services")):
                whole_packages += 1
                package_ids.add(str(entry.get("service_id") or ""))
            else:
                plain += 1
    if not package_ids:
        return
    if len(package_ids) > 1 or whole_packages > 1:
        raise SessionValidationError("Only one package can be booked at a time.")
    if plain:
        raise SessionValidationError("A package cannot be combined with other services in one booking.")
