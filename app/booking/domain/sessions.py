"""
app/booking/domain/sessions.py
──────────────────────────────
Pure helpers for the multi-session booking model.

A booking owns 1..MAX_SESSIONS_PER_BOOKING *sessions* stored in ``bookings.sessions``
(JSONB).  A session is served by exactly one therapist (plus an optional secondary
therapist for couple/double rooms) in exactly one arrangement and contains one or
more services, add-ons and extra minutes.  All sessions of a booking happen on the
same (branch-local) date and must not overlap.

The legacy single-session columns of ``bookings`` stay populated (see
:func:`legacy_mirror`) so existing consumers keep working unchanged.  Every helper
is side-effect free and tolerant of partially populated rows (legacy rows, test
doubles) – they never raise on malformed *stored* data.  Only
:func:`validate_sessions` raises, on invalid *input*.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.booking.domain.services_snapshot import (
    _to_decimal,
    _to_int,
    _to_str_id,
    get_booking_services,
)
from app.common.utils import to_local_tz

MAX_SESSIONS_PER_BOOKING = 3
# Bookings that include a package booked by its parts may use more sessions.
MAX_PACKAGE_SESSIONS_PER_BOOKING = 5


class SessionValidationError(ValueError):
    """Raised when the requested sessions are inconsistent (maps to HTTP 422)."""


def _money(value: Decimal) -> str:
    return f"{value:.3f}"


def compute_extra_price(extra_minutes: int, unit_minutes: Any, unit_price: Any) -> Decimal:
    """
    Price of ``extra_minutes`` using the service's own extra-time rate
    (``extra_minutes`` unit × ``price_for_extra_minutes``).  Never derived from the
    service price.  Returns 0 when the service has no extra-time rate.
    """
    mins = max(_to_int(extra_minutes), 0)
    rate = _to_decimal(unit_price)
    if mins == 0 or rate <= 0:
        return Decimal("0")
    unit = _to_int(unit_minutes) or 30
    return (Decimal(mins) / Decimal(unit)) * rate


def normalise_addons(addons: list[Any] | None) -> list[dict[str, Any]]:
    """Normalise add-on dicts/objects to ``{addon_id, name, price, duration_minutes}``."""
    out: list[dict[str, Any]] = []
    for a in addons or []:
        get = a.get if isinstance(a, dict) else (lambda k, d=None, _a=a: getattr(_a, k, d))
        addon_id = _to_str_id(get("addon_id")) or _to_str_id(get("id")) or ""
        out.append(
            {
                "addon_id": addon_id,
                "name": str(get("name", "") or ""),
                "price": _money(_to_decimal(get("price"))),
                "duration_minutes": _to_int(get("duration_minutes") or get("duration")),
            }
        )
    return out


def build_session(
    *,
    position: int,
    start: datetime,
    therapist_id: Any,
    therapist_data: dict | None,
    arrangement_id: Any,
    arrangement_data: dict | None,
    services: list[dict[str, Any]],
    addons: list[Any] | None = None,
    extra_minutes: int = 0,
    extra_unit_minutes: Any = None,
    extra_unit_price: Any = None,
    secondary_therapist_id: Any = None,
    secondary_therapist_data: dict | None = None,
    tertiary_therapist_id: Any = None,
    tertiary_therapist_data: dict | None = None,
    session_id: Any = None,
) -> dict[str, Any]:
    """
    Build one stored session.  Durations, prices and ``appointment_end`` are always
    computed here (server side); ``start`` must be timezone-aware.
    """
    if start.tzinfo is None:
        raise SessionValidationError("Session start must carry timezone info.")
    if not services:
        raise SessionValidationError("A session needs at least one service.")

    entries = []
    for i, e in enumerate(services):
        e = dict(e)
        e["position"] = i
        e["service_arrangement_id"] = _to_str_id(arrangement_id) or e.get("service_arrangement_id")
        entries.append(e)

    addon_list = normalise_addons(addons)
    services_duration = sum(_to_int(e.get("duration_minutes")) for e in entries)
    services_price = sum((_to_decimal(e.get("base_price")) for e in entries), Decimal("0"))
    addons_duration = sum(a["duration_minutes"] for a in addon_list)
    addons_price = sum((_to_decimal(a["price"]) for a in addon_list), Decimal("0"))
    extra = max(_to_int(extra_minutes), 0)
    extra_price = compute_extra_price(extra, extra_unit_minutes, extra_unit_price)
    total_duration = services_duration + addons_duration + extra
    start_local = to_local_tz(start)
    end_local = start_local + timedelta(minutes=total_duration)
    sid = _to_str_id(session_id) or str(uuid.uuid4())

    session_service_names = [
        str(
            (e.get("service_data") or {}).get("name")
            or (e.get("service_data") or {}).get("service_name")
            or e.get("name")
            or e.get("service_name")
            or ""
        )
        for e in entries
    ]
    session_service_name = " + ".join(n for n in session_service_names if n)

    return {
        "session_id": sid,
        "position": position,
        "therapist_id": _to_str_id(therapist_id),
        "therapist_data": dict(therapist_data or {}),
        "secondary_therapist_id": _to_str_id(secondary_therapist_id),
        "secondary_therapist_data": dict(secondary_therapist_data or {}) if secondary_therapist_id else None,
        "tertiary_therapist_id": _to_str_id(tertiary_therapist_id),
        "tertiary_therapist_data": dict(tertiary_therapist_data or {}) if tertiary_therapist_id else None,
        "service_arrangement_id": _to_str_id(arrangement_id),
        "service_arrangement_data": dict(arrangement_data or {}),
        "service_name": session_service_name,
        "services": entries,
        "service_ids": [e.get("service_id") for e in entries if e.get("service_id")],
        "addons": addon_list,
        "extra_minutes": extra,
        "price_for_extra_minutes": _money(extra_price),
        "services_duration": services_duration,
        "addons_duration": addons_duration,
        "total_session_duration": total_duration,
        "services_price": _money(services_price),
        "addons_price": _money(addons_price),
        "session_price": _money(services_price + addons_price + extra_price),
        "appointment_start": start_local.isoformat(),
        "appointment_end": end_local.isoformat(),
    }


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return to_local_tz(value)
    if isinstance(value, str) and value.strip():
        try:
            return to_local_tz(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def session_window(session: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    return _parse_dt(session.get("appointment_start")), _parse_dt(session.get("appointment_end"))


def sort_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by start time and renumber ``position`` (0-based)."""
    ordered = sorted(
        sessions,
        key=lambda s: (session_window(s)[0] or datetime.max.replace(tzinfo=timezone.utc)),
    )
    for i, s in enumerate(ordered):
        s["position"] = i
    return ordered


def validate_sessions(
    sessions: list[dict[str, Any]],
    *,
    max_sessions: int = MAX_SESSIONS_PER_BOOKING,
) -> list[dict[str, Any]]:
    """
    Validate and return the sessions sorted by start time.

    Rules: 1..max sessions; every session has a therapist, an arrangement-or-none,
    services and a tz-aware window; same branch-local date for all sessions; no overlap
    (``end[i] <= start[i+1]``); the same therapist may not be used in overlapping windows
    (implied by no-overlap).
    """
    if not sessions:
        raise SessionValidationError("At least one session is required.")
    if len(sessions) > max_sessions:
        raise SessionValidationError(f"A booking can contain at most {max_sessions} sessions.")

    for i, s in enumerate(sessions):
        if not s.get("therapist_id"):
            raise SessionValidationError(f"sessions[{i}]: therapist_id is required.")
        if not s.get("services"):
            raise SessionValidationError(f"sessions[{i}]: at least one service is required.")
        start, end = session_window(s)
        if start is None or end is None or end <= start:
            raise SessionValidationError(f"sessions[{i}]: invalid start/end time.")

    ordered = sort_sessions(list(sessions))
    first_start = session_window(ordered[0])[0]
    assert first_start is not None
    day = first_start.date()
    prev_end: datetime | None = None
    for i, s in enumerate(ordered):
        start, end = session_window(s)
        assert start is not None and end is not None
        if start.date() != day or end.date() != day:
            raise SessionValidationError(
                f"sessions[{i}]: all sessions of a booking must be on the same date ({day.isoformat()})."
            )
        if prev_end is not None and start < prev_end:
            raise SessionValidationError(f"sessions[{i}]: overlaps the previous session.")
        prev_end = end
    return ordered


def flatten_services(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All services of all sessions in order, each tagged with its ``session_id``."""
    out: list[dict[str, Any]] = []
    for s in sessions:
        for e in s.get("services") or []:
            item = dict(e)
            item["session_id"] = s.get("session_id")
            item["position"] = len(out)
            out.append(item)
    return out


def flatten_addons(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in sessions:
        for a in s.get("addons") or []:
            item = dict(a)
            item["session_id"] = s.get("session_id")
            out.append(item)
    return out


def booking_totals(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Booking level sums computed from sessions."""
    services_price = sum((_to_decimal(s.get("services_price")) for s in sessions), Decimal("0"))
    addons_price = sum((_to_decimal(s.get("addons_price")) for s in sessions), Decimal("0"))
    extra_price = sum((_to_decimal(s.get("price_for_extra_minutes")) for s in sessions), Decimal("0"))
    return {
        "services_price": services_price,
        "addons_price": addons_price,
        "extra_price": extra_price,
        "session_price_total": sum((_to_decimal(s.get("session_price")) for s in sessions), Decimal("0")),
        "services_duration": sum(_to_int(s.get("services_duration")) for s in sessions),
        "addons_duration": sum(_to_int(s.get("addons_duration")) for s in sessions),
        "extra_minutes": sum(_to_int(s.get("extra_minutes")) for s in sessions),
        "total_duration": sum(_to_int(s.get("total_session_duration")) for s in sessions),
    }


def legacy_mirror(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Legacy single-session column values derived from the (sorted, validated) sessions:
    therapist / arrangement / primary service from session 0, window from first start to
    last end, durations and extra minutes summed, add-ons and services flattened.
    """
    first, last = sessions[0], sessions[-1]
    first_start = session_window(first)[0]
    last_end = session_window(last)[1]
    totals = booking_totals(sessions)
    return {
        "therapist_id": first.get("therapist_id"),
        "therapist_data": first.get("therapist_data") or {},
        "secondary_therapist_id": first.get("secondary_therapist_id"),
        "secondary_therapist_data": first.get("secondary_therapist_data"),
        "tertiary_therapist_id": first.get("tertiary_therapist_id"),
        "tertiary_therapist_data": first.get("tertiary_therapist_data"),
        "service_arrangement_id": first.get("service_arrangement_id"),
        "service_arrangement_data": first.get("service_arrangement_data") or {},
        "services": flatten_services(sessions),
        "addons": flatten_addons(sessions),
        "appointment_start": first_start,
        "appointment_end": last_end,
        "duration_minutes": totals["services_duration"],
        "extra_minutes": totals["extra_minutes"],
        "addons_duration": totals["addons_duration"],
        "total_duration": totals["total_duration"],
        **{k: totals[k] for k in ("services_price", "addons_price", "extra_price")},
    }


def session_from_booking(booking: Any) -> dict[str, Any]:
    """Synthesize the single session of a legacy booking (no stored sessions)."""
    entries = get_booking_services(booking)
    start = getattr(booking, "appointment_start", None)
    end = getattr(booking, "appointment_end", None)
    addons = [a for a in (getattr(booking, "addons", None) or []) if isinstance(a, dict)]
    base_price = sum((_to_decimal(e.get("base_price")) for e in entries), Decimal("0"))
    services_duration = sum(_to_int(e.get("duration_minutes")) for e in entries)
    dur = getattr(booking, "total_duration", None)
    if not isinstance(dur, int):
        dur = _to_int(getattr(booking, "duration_minutes", 0)) + _to_int(
            getattr(booking, "extra_minutes", 0)
        )
    addons_dur = getattr(booking, "addons_duration", None)
    addons_dur = addons_dur if isinstance(addons_dur, int) else 0
    extra_price = _to_decimal(getattr(booking, "price_for_extra_minutes", 0))
    addon_price = _to_decimal(getattr(booking, "addon_price", 0))
    sec_id = getattr(booking, "secondary_therapist_id", None)
    tert_id = getattr(booking, "tertiary_therapist_id", None)
    arr_id = _to_str_id(getattr(booking, "service_arrangement_id", None))
    sid = _to_str_id(getattr(booking, "id", None))
    return {
        "session_id": sid or str(uuid.uuid4()),
        "position": 0,
        "therapist_id": _to_str_id(getattr(booking, "therapist_id", None)),
        "therapist_data": dict(getattr(booking, "therapist_data", None) or {}),
        "secondary_therapist_id": _to_str_id(sec_id),
        "secondary_therapist_data": getattr(booking, "secondary_therapist_data", None) if sec_id else None,
        "tertiary_therapist_id": _to_str_id(tert_id),
        "tertiary_therapist_data": getattr(booking, "tertiary_therapist_data", None) if tert_id else None,
        "service_arrangement_id": arr_id,
        "service_arrangement_data": dict(getattr(booking, "service_arrangement_data", None) or {}),
        "service_name": getattr(booking, "service_name", None) or ", ".join(filter(None, (e.get("name") or (e.get("service_data") or {}).get("name") or "" for e in entries))),
        "services": [dict(e) for e in entries],
        "service_ids": [e.get("service_id") for e in entries if e.get("service_id")],
        "addons": normalise_addons(addons),
        "extra_minutes": _to_int(getattr(booking, "extra_minutes", 0)),
        "price_for_extra_minutes": _money(extra_price),
        "services_duration": services_duration,
        "addons_duration": addons_dur,
        "total_session_duration": dur,
        "services_price": _money(base_price),
        "addons_price": _money(addon_price),
        "session_price": _money(base_price + addon_price + extra_price),
        "appointment_start": start.isoformat() if isinstance(start, datetime) else None,
        "appointment_end": end.isoformat() if isinstance(end, datetime) else None,
    }


def get_booking_sessions(booking: Any) -> list[dict[str, Any]]:
    """Stored sessions of a booking, or one synthesized from the legacy columns."""
    raw = getattr(booking, "sessions", None)
    if isinstance(raw, list) and raw and all(isinstance(s, dict) for s in raw):
        return raw
    return [session_from_booking(booking)]


def therapist_busy_windows(booking: Any, therapist_id: Any) -> list[tuple[datetime, datetime]]:
    """
    Windows during which ``therapist_id`` is actually occupied by ``booking``.

    A therapist is only blocked for the sessions he/she serves (as primary or
    secondary therapist) – never for the whole booking span.  Legacy bookings
    (no stored sessions) block their therapists for the full booking window.
    """
    tid = _to_str_id(therapist_id)
    if not tid:
        return []
    raw = getattr(booking, "sessions", None)
    windows: list[tuple[datetime, datetime]] = []
    if isinstance(raw, list) and raw and all(isinstance(s, dict) for s in raw):
        for s in raw:
            if tid not in (_to_str_id(s.get("therapist_id")), _to_str_id(s.get("secondary_therapist_id")), _to_str_id(s.get("tertiary_therapist_id"))):
                continue
            s_start, s_end = session_window(s)
            if s_start and s_end:
                windows.append((s_start, s_end))
        return windows
    ids = {
        _to_str_id(getattr(booking, "therapist_id", None)),
        _to_str_id(getattr(booking, "secondary_therapist_id", None)),
        _to_str_id(getattr(booking, "tertiary_therapist_id", None)),
    }
    start = getattr(booking, "appointment_start", None)
    end = getattr(booking, "appointment_end", None)
    if tid in ids and start and end:
        windows.append((start, end))
    return windows


def distinct_session_arrangements(booking: Any) -> int:
    """Number of distinct arrangements across the booking's legacy service entries."""
    ids = {
        _to_str_id(e.get("service_arrangement_id"))
        for e in get_booking_services(booking)
        if _to_str_id(e.get("service_arrangement_id"))
    }
    return len(ids)


def stored_sessions(booking: Any) -> list[dict[str, Any]] | None:
    """The booking's *persisted* sessions, or ``None`` for legacy rows."""
    raw = getattr(booking, "sessions", None)
    if isinstance(raw, list) and raw and all(isinstance(s, dict) for s in raw):
        return raw
    return None


def cache_payload_for(booking: Any) -> list[dict[str, Any]]:
    """
    ``sessions`` payload for ushauth's appointment cache.

    Only bookings with persisted sessions switch ushauth to per-session rows; legacy
    bookings (incl. legacy multi-room multi-service ones) return ``[]`` so ushauth keeps
    its existing per-arrangement behaviour.  Never raises.
    """
    try:
        sessions = stored_sessions(booking)
        return cache_sessions_payload(sessions) if sessions else []
    except Exception:  # noqa: BLE001 – cache sync must never break a booking flow
        return []


def cache_sessions_payload(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact per-session payload sent to ushauth's appointment cache."""
    out = []
    for s in sessions:
        sd = s.get("therapist_data") or {}
        sec = s.get("secondary_therapist_data") or {}
        tert = s.get("tertiary_therapist_data") or {}
        ad = s.get("service_arrangement_data") or {}
        names = [
            str((e.get("service_data") or {}).get("name") or (e.get("service_data") or {}).get("service_name") or "")
            for e in s.get("services") or []
        ]
        out.append(
            {
                "session_id": str(s["session_id"]) if s.get("session_id") else None,
                "position": _to_int(s.get("position")),
                "therapist_id": str(s["therapist_id"]) if s.get("therapist_id") else None,
                "therapist_name": str(sd.get("name") or sd.get("full_name") or sd.get("therapist_name") or ""),
                "secondary_therapist_id": str(s["secondary_therapist_id"]) if s.get("secondary_therapist_id") else None,
                "secondary_therapist_name": str(
                    sec.get("name") or sec.get("full_name") or sec.get("therapist_name") or ""
                ),
                "tertiary_therapist_id": str(s["tertiary_therapist_id"]) if s.get("tertiary_therapist_id") else None,
                "tertiary_therapist_name": str(
                    tert.get("name") or tert.get("full_name") or tert.get("therapist_name") or ""
                ),
                "service_arrangement_id": str(s["service_arrangement_id"]) if s.get("service_arrangement_id") else None,
                "service_arrangement_name": str(
                    ad.get("arrangement_name") or ad.get("room_name") or ad.get("name") or ""
                ),
                "service_ids": [str(x) for x in (s.get("service_ids") or []) if x is not None],
                "service_name": ", ".join(n for n in names if n),
                "appointment_start": s.get("appointment_start"),
                "appointment_end": s.get("appointment_end"),
                "duration_minutes": _to_int(s.get("total_session_duration")) or _to_int(s.get("duration_minutes")) or _to_int(s.get("duration")) or 60,
            }
        )
    return out
