"""
app/booking/application/session_planner.py
──────────────────────────────────────────
Turns a ``sessions[]`` request into validated, fully computed stored sessions.

For every requested session this module (concurrently across sessions):

1. resolves its services against ushauth (full service details are stored),
2. snapshots the therapist(s) and arrangement add-ons,
3. computes durations, prices (server side – never trusting client totals; extra minutes
   are priced from the service's own ``extra_minutes`` / ``price_for_extra_minutes``) and
   a tz-aware start / end,

then validates the set (≤ 3 sessions, same date, no overlap) and – for branch bookings –
checks every session against ushauth availability (arrangement **and** therapist for the
whole session length; ``exclude_booking_id`` lets an update ignore its own rows).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.booking.application.multi_service import resolve_service_entries
from app.booking.application.package_rules import (
    apply_package_context,
    resolve_package_context,
    validate_package_exclusivity,
)
from app.booking.application.multi_therapist import (
    entries_required_therapists,
    parse_uuid,
    therapist_display_name,
    unique_ids,
)
from app.booking.domain.services_snapshot import _to_decimal, _to_int
from app.booking.domain.sessions import (
    MAX_PACKAGE_SESSIONS_PER_BOOKING,
    MAX_SESSIONS_PER_BOOKING,
    SessionValidationError,
    booking_totals,
    build_session,
    flatten_services,
    legacy_mirror,
    session_window,
    validate_sessions,
)
from app.common.utils import to_local_tz
from app.core.logging import get_logger

logger = get_logger(__name__)


class SessionAvailabilityError(Exception):
    """A session's arrangement/therapist is not free (maps to HTTP 409)."""


@dataclass
class SessionPlan:
    sessions: list[dict[str, Any]]
    mirror: dict[str, Any]
    totals: dict[str, Any]
    service_entries: list[dict[str, Any]] = field(default_factory=list)


def _unwrap(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        return data if isinstance(data, dict) else {}
    return {}


def _start_of(item: Any, appointment_date: str) -> datetime:
    """Tz-aware (Asia/Kuwait) start of a session from the fixed booking date + its time."""
    if item.appointment_time:
        try:
            naive = datetime.fromisoformat(f"{appointment_date}T{item.appointment_time}")
        except ValueError as exc:
            raise SessionValidationError(f"Invalid session time '{item.appointment_time}'.") from exc
        return to_local_tz(naive)
    start = to_local_tz(item.appointment_start)
    if start is None:
        raise SessionValidationError("Each session needs appointment_time or appointment_start.")
    return start


async def _therapist_snapshot(
    ushauth: Any, therapist_id: uuid.UUID | str | None, provided: dict | None, name: str | None
) -> dict[str, Any]:
    tid = str(therapist_id) if therapist_id else ""
    data: dict[str, Any] = dict(provided or {})
    nm = name or therapist_display_name(data)
    avatar = data.get("avatar") or data.get("photo_url")
    if tid and (not nm or not avatar):
        try:
            profile = _unwrap(await ushauth.get_therapist(tid))
            nm = nm or therapist_display_name(profile)
            avatar = avatar or profile.get("avatar") or profile.get("photo_url")
        except Exception as exc:  # cosmetic – never block a booking
            logger.warning("session_therapist_lookup_failed", therapist_id=tid, error=str(exc))
    snap = {"id": tid, "name": nm, "therapist_name": nm, "full_name": nm, **data}
    if avatar:
        snap.setdefault("avatar", avatar)
        snap.setdefault("photo_url", avatar)
    return snap


def _enrich_addons(raw_addons: list[dict[str, Any]] | None, db_arrangement: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer ushauth add-on name/price/duration (source of truth) over client values."""
    db_by_id = {
        str(a.get("id") or a.get("addon_id")): a
        for a in (db_arrangement.get("addons") or [])
        if isinstance(a, dict)
    }
    out: list[dict[str, Any]] = []
    for a in raw_addons or []:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("addon_id") or a.get("id") or "")
        db = db_by_id.get(aid)
        merged = dict(a)
        if db:
            merged["name"] = db.get("name") or a.get("name")
            if db.get("price") is not None:
                merged["price"] = db.get("price")
            if db.get("duration_minutes") is not None:
                merged["duration_minutes"] = db.get("duration_minutes")
        out.append(merged)
    return out


async def _build_one_session(
    ushauth: Any,
    item: Any,
    position: int,
    appointment_date: str,
    is_loyalty_booking: bool,
    package_ctx: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    arr_id = item.service_arrangement_id
    svc_items = [
        s.model_copy(update={"service_arrangement_id": arr_id or s.service_arrangement_id})
        for s in item.services
    ]
    # Every service in a session must be offered by the session's arrangement.
    entries = await resolve_service_entries(
        ushauth, svc_items, is_loyalty_booking=is_loyalty_booking, full_details=True, validate=True
    )
    apply_package_context(entries, svc_items, position, package_ctx or {})

    db_arr: dict[str, Any] = {}
    if arr_id:
        try:
            db_arr = _unwrap(await ushauth.get_service_arrangement(str(arr_id)))
        except Exception as exc:
            logger.warning("session_arrangement_lookup_failed", arrangement_id=str(arr_id), error=str(exc))

    arr_data = {
        "id": str(arr_id) if arr_id else "",
        "arrangement_type": item.arrangement_type or db_arr.get("arrangement_type") or "",
        "room_name": item.room_name or item.arrangement_name or db_arr.get("name") or "",
        "arrangement_name": item.arrangement_name or item.room_name or db_arr.get("name") or "",
        **(item.service_arrangement_data or {}),
    }
    if not arr_data.get("arrangement_type"):
        arr_data["arrangement_type"] = (entries[0].get("service_arrangement_data") or {}).get("arrangement_type", "")

    therapist_data, secondary_data = await asyncio.gather(
        _therapist_snapshot(ushauth, item.therapist_id, item.therapist_data, item.therapist_name),
        _therapist_snapshot(
            ushauth, item.secondary_therapist_id, item.secondary_therapist_data, item.secondary_therapist_name
        )
        if item.secondary_therapist_id
        else asyncio.sleep(0, result=None),
    )

    primary_sd = entries[0].get("service_data") or {}
    return build_session(
        position=position,
        start=_start_of(item, appointment_date),
        therapist_id=item.therapist_id,
        therapist_data=therapist_data,
        arrangement_id=arr_id,
        arrangement_data=arr_data,
        services=entries,
        addons=_enrich_addons(item.addons, db_arr),
        extra_minutes=item.extra_minutes,
        extra_unit_minutes=primary_sd.get("extra_minutes"),
        extra_unit_price=primary_sd.get("price_for_extra_minutes"),
        secondary_therapist_id=item.secondary_therapist_id,
        secondary_therapist_data=secondary_data,
        session_id=item.session_id,
    )


async def _check_session_availability(
    ushauth: Any,
    session: dict[str, Any],
    *,
    appointment_date: str,
    branch_id: str | None,
    exclude_booking_id: str | None,
) -> None:
    """Raise SessionAvailabilityError if the session's room / therapist(s) are not free."""
    arr_id = session.get("service_arrangement_id")
    if not arr_id:
        # Roomless session (e.g. home service): only the therapist(s) need to be free.
        # Skipped on edits – the endpoint cannot exclude the booking's own rows.
        if exclude_booking_id:
            return
        r_start, _ = session_window(session)
        assert r_start is not None
        for tid in unique_ids([session.get("therapist_id"), session.get("secondary_therapist_id")]):
            res = await ushauth.check_booking_therapist_availability(
                therapist_id=str(tid),
                appointment_date=appointment_date,
                appointment_time=r_start.strftime("%H:%M:%S"),
                duration=_to_int(session.get("total_session_duration")),
            )
            if res.get("available") != "yes":
                raise SessionAvailabilityError(
                    f"Session {int(session.get('position', 0)) + 1}: the selected therapist is not "
                    f"available at {r_start.strftime('%H:%M')}."
                )
        return
    start, _ = session_window(session)
    assert start is not None
    arr_data = session.get("service_arrangement_data") or {}
    required = entries_required_therapists([{"service_arrangement_data": arr_data}])
    therapist_ids = unique_ids(
        [session.get("therapist_id"), session.get("secondary_therapist_id")]
    )
    res = await ushauth.check_appointment_availability(
        service_arrangement_id=str(arr_id),
        appointment_date=appointment_date,
        appointment_time=start.strftime("%H:%M:%S"),
        duration=_to_int(session.get("total_session_duration")),
        therapist_id=session.get("therapist_id"),
        therapist_ids=therapist_ids if len(therapist_ids) > 1 else None,
        required_therapists=required,
        branch_id=branch_id,
        service_id=(session.get("service_ids") or [None])[0],
        exclude_booking_id=exclude_booking_id,
        allow_single_therapist=required > 1 and not session.get("secondary_therapist_id"),
    )
    if res.get("available") != "yes":
        raise SessionAvailabilityError(
            f"Session {int(session.get('position', 0)) + 1}: the selected room or therapist is not "
            f"available at {start.strftime('%H:%M')}."
        )
    # ushauth is authoritative about the therapist(s) actually reserved.
    chosen = res.get("therapist_id")
    if chosen and str(chosen) != str(session.get("therapist_id")):
        session["therapist_id"] = str(chosen)
    if required > 1 and not session.get("secondary_therapist_id"):
        ids = unique_ids([chosen, *(res.get("therapist_ids") or []), res.get("secondary_therapist_id")])
        sec = parse_uuid(ids[1]) if len(ids) > 1 else None
        if sec and str(sec) != str(session.get("therapist_id")):
            session["secondary_therapist_id"] = str(sec)
            session["secondary_therapist_data"] = {"id": str(sec)}


async def plan_sessions(
    ushauth: Any,
    items: list[Any],
    *,
    appointment_date: str,
    is_loyalty_booking: bool = False,
    check_availability: bool = True,
    branch_id: str | None = None,
    exclude_booking_id: str | None = None,
) -> SessionPlan:
    """
    Build + validate the stored sessions for a request.

    Raises :class:`SessionValidationError` (422), :class:`SessionAvailabilityError` (409);
    ushauth gateway errors propagate to the caller unchanged.
    """
    from app.booking.application.multi_service import ServiceValidationError

    try:
        package_ctx = await resolve_package_context(ushauth, items)
        built = await asyncio.gather(
            *(
                _build_one_session(ushauth, it, idx, appointment_date, is_loyalty_booking, package_ctx)
                for idx, it in enumerate(items)
            )
        )
    except ServiceValidationError as exc:
        raise SessionValidationError(str(exc)) from exc

    validate_package_exclusivity(list(built))
    sessions = validate_sessions(
        list(built),
        max_sessions=MAX_PACKAGE_SESSIONS_PER_BOOKING if package_ctx else MAX_SESSIONS_PER_BOOKING,
    )
    day = appointment_date.split("T")[0]
    if session_window(sessions[0])[0].date().isoformat() != day:  # type: ignore[union-attr]
        raise SessionValidationError(f"Session times must be on the booking date ({day}).")

    if check_availability:
        # Sessions never overlap, so their checks are independent → run concurrently.
        await asyncio.gather(
            *(
                _check_session_availability(
                    ushauth,
                    s,
                    appointment_date=day,
                    branch_id=branch_id,
                    exclude_booking_id=exclude_booking_id,
                )
                for s in sessions
            )
        )

    return SessionPlan(
        sessions=sessions,
        mirror=legacy_mirror(sessions),
        totals=booking_totals(sessions),
        service_entries=flatten_services(sessions),
    )


def totals_as_decimals(plan: SessionPlan) -> tuple[Decimal, Decimal, Decimal]:
    """(services_price, addons_price, extra_price) of a plan."""
    t = plan.totals
    return (
        _to_decimal(t["services_price"]),
        _to_decimal(t["addons_price"]),
        _to_decimal(t["extra_price"]),
    )

