"""
Multi-therapist helpers.

Couple / double rooms are used by two guests taking the same service at the same
time, so such a booking carries **two** therapists: the primary one
(``Booking.therapist_id``, unchanged for every existing client) and a secondary
one (``Booking.secondary_therapist_id``).
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

#: Arrangement types that need two therapists (mirrors ushauth).
MULTI_THERAPIST_ARRANGEMENT_TYPES: frozenset[str] = frozenset({"couple_room", "double_room"})


def required_therapist_count(arrangement_type: Any) -> int:
    """Therapists needed for an arrangement type (2 for couple/double rooms, else 1)."""
    return 2 if str(arrangement_type or "").strip().lower() in MULTI_THERAPIST_ARRANGEMENT_TYPES else 1


def entries_required_therapists(service_entries: Iterable[dict[str, Any]] | None) -> int:
    """Highest therapist requirement across the arrangements and services of a booking (1 to 3)."""
    needed = 1
    for entry in service_entries or []:
        arr_data = (entry or {}).get("service_arrangement_data") or {}
        arr_type = arr_data.get("arrangement_type") or arr_data.get("type")
        svc_data = (entry or {}).get("service_data") or {}
        raw_svc_needed = svc_data.get("no_of_therapists_needed") or 1
        try:
            svc_needed = int(raw_svc_needed)
        except (ValueError, TypeError):
            svc_needed = 1
        needed = max(needed, required_therapist_count(arr_type), svc_needed)
    return min(max(needed, 1), 3)


def unique_ids(ids: Iterable[Any]) -> list[str]:
    """De-duplicate IDs (as strings) while keeping the caller's priority order."""
    seen: dict[str, None] = {}
    for raw in ids:
        if raw:
            seen.setdefault(str(raw), None)
    return list(seen)


def therapist_display_name(raw: dict[str, Any] | None) -> str:
    """Best-effort display name from a ushauth therapist payload or a stored snapshot."""
    if not raw:
        return ""
    name = raw.get("therapist_name") or raw.get("name") or raw.get("full_name")
    if name:
        return str(name)
    return f"{raw.get('first_name', '')} {raw.get('last_name', '')}".strip()


def parse_uuid(value: Any) -> uuid.UUID | None:
    """Return a UUID or ``None`` when the value is empty/invalid."""
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError, TypeError):
        return None
