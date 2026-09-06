"""
app/common/utils.py
───────────────────
Shared utility functions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def new_uuid() -> uuid.UUID:
    """Return a new UUID4."""
    return uuid.uuid4()


def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(tz=ZoneInfo("UTC"))


def local_today(timezone: str) -> date:
    """Return today's date in the given IANA timezone string."""
    tz = ZoneInfo(timezone)
    return datetime.now(tz=tz).date()


def date_range(start: date, days: int) -> list[date]:
    """Return a list of *days* dates starting from *start* (inclusive)."""
    return [start + timedelta(days=i) for i in range(days)]


def combine_date_time(d: date, t: time, timezone: str) -> datetime:
    """Combine a date and time into a timezone-aware datetime."""
    tz = ZoneInfo(timezone)
    return datetime.combine(d, t, tzinfo=tz)


def to_utc(dt: datetime) -> datetime:
    """Convert a timezone-aware datetime to UTC."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(ZoneInfo("UTC"))
