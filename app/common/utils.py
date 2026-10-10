"""
app/common/utils.py
───────────────────
Shared utility functions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
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


DEFAULT_TIMEZONE = ZoneInfo("Asia/Kuwait")


def to_local_tz(dt: datetime | None, tz: ZoneInfo = DEFAULT_TIMEZONE) -> datetime | None:
    """
    Return *dt* as a timezone-aware datetime in the platform timezone (Asia/Kuwait).

    - Naive datetimes are interpreted as Asia/Kuwait local time.
    - Aware datetimes (e.g. UTC values returned by the database driver) are
      converted to the same instant expressed in Asia/Kuwait.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def local_now(tz: ZoneInfo = DEFAULT_TIMEZONE) -> datetime:
    """Current instant as a timezone-aware datetime in Asia/Kuwait."""
    return datetime.now(tz)


def local_wallclock_now(tz: ZoneInfo = DEFAULT_TIMEZONE) -> datetime:
    """Backward-compatible alias of :func:`local_now` (real, timezone-aware 'now')."""
    return local_now(tz)


def combine_local(d: date, t: time, tz: ZoneInfo = DEFAULT_TIMEZONE) -> datetime:
    """Combine a local date and time into an Asia/Kuwait timezone-aware datetime."""
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=tz)


def local_midnight(d: date | datetime, tz: ZoneInfo = DEFAULT_TIMEZONE) -> datetime:
    """Midnight (00:00) of the given local date as an Asia/Kuwait timezone-aware datetime."""
    if isinstance(d, datetime):
        d = to_local_tz(d, tz).date()
    return datetime.combine(d, time(0, 0), tzinfo=tz)


# ── Shared API serialisation type ─────────────────────────────────────────
# Standard for every API response across USH services: ISO-8601 with an
# explicit offset, rendered in the business timezone (Asia/Kuwait, +03:00).
# Validation is untouched (any ISO input accepted; naive = Kuwait wall-clock
# where callers use to_local_tz); only JSON output is normalised.
from typing import Annotated  # noqa: E402

from pydantic import PlainSerializer  # noqa: E402


def _serialize_local(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        return dt  # type: ignore[return-value]
    return to_local_tz(dt).isoformat()


LocalDateTime = Annotated[datetime, PlainSerializer(_serialize_local, when_used="json")]
