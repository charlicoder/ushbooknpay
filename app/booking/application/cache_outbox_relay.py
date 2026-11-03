"""
app/booking/application/cache_outbox_relay.py
──────────────────────────────────────────────
Enqueue + replay of failed ushauth appointment-cache requests.

See ``app/booking/infrastructure/cache_outbox.py`` for the rationale.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, exists, select, update
from sqlalchemy.orm import aliased

from app.booking.infrastructure.cache_outbox import (
    DEAD,
    DELIVERED,
    PENDING,
    AppointmentCacheOutbox,
)
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger

logger = get_logger(__name__)

_LEASE_SECONDS = 90
_MAX_BACKOFF_SECONDS = 15 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff: 10s, 20s, 40s … capped at 15 minutes."""
    return min(10 * (2 ** max(attempts - 1, 0)), _MAX_BACKOFF_SECONDS)


async def enqueue_cache_request(path: str, payload: dict[str, Any], booking_id: str, error: str = "") -> None:
    """Persist a failed ushauth cache request for later replay (never raises)."""
    try:
        settings = get_settings()
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                AppointmentCacheOutbox(
                    booking_id=str(booking_id),
                    path=path,
                    payload=payload,
                    status=PENDING,
                    attempts=1,  # the synchronous attempt already failed
                    max_attempts=settings.CACHE_OUTBOX_MAX_ATTEMPTS,
                    last_error=error[:1000],
                    next_attempt_at=_utcnow() + timedelta(seconds=backoff_seconds(1)),
                )
            )
            await session.commit()
        logger.warning("cache_outbox_enqueued", booking_id=str(booking_id), path=path)
    except Exception as exc:  # pragma: no cover - defensive, must never mask the caller's error
        logger.error("cache_outbox_enqueue_failed", booking_id=str(booking_id), path=path, error=str(exc))


async def has_pending_for_booking(booking_id: str) -> bool:
    """True when an earlier request for this booking is still waiting in the outbox."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.execute(
                select(AppointmentCacheOutbox.id)
                .where(
                    AppointmentCacheOutbox.booking_id == str(booking_id),
                    AppointmentCacheOutbox.status == PENDING,
                )
                .limit(1)
            )
            return row.first() is not None
    except Exception as exc:  # pragma: no cover
        logger.error("cache_outbox_pending_check_failed", booking_id=str(booking_id), error=str(exc))
        return False


async def _claim_due_batch(limit: int) -> list[AppointmentCacheOutbox]:
    """
    Lease up to ``limit`` due rows (``SKIP LOCKED`` + pushing ``next_attempt_at``
    forward) so multiple replicas never replay the same row concurrently.

    A row is only claimable if no *older* pending row of the same booking exists,
    which preserves per-booking ordering (create before status updates).
    """
    factory = get_session_factory()
    now = _utcnow()
    older = aliased(AppointmentCacheOutbox)
    async with factory() as session:
        stmt = (
            select(AppointmentCacheOutbox)
            .where(
                AppointmentCacheOutbox.status == PENDING,
                AppointmentCacheOutbox.next_attempt_at <= now,
                ~exists().where(
                    and_(
                        older.booking_id == AppointmentCacheOutbox.booking_id,
                        older.status == PENDING,
                        older.created_at < AppointmentCacheOutbox.created_at,
                    )
                ),
            )
            .order_by(AppointmentCacheOutbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return []
        lease_until = now + timedelta(seconds=_LEASE_SECONDS)
        await session.execute(
            update(AppointmentCacheOutbox)
            .where(AppointmentCacheOutbox.id.in_([r.id for r in rows]))
            .values(next_attempt_at=lease_until)
        )
        await session.commit()
        return rows


async def _finish(row_id: Any, **values: Any) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(AppointmentCacheOutbox).where(AppointmentCacheOutbox.id == row_id).values(**values)
        )
        await session.commit()


async def process_outbox_once(client: Any, batch_size: int | None = None) -> dict[str, int]:
    """
    Replay one batch of due outbox rows through ``client._post``.

    Returns counters ``{"delivered", "retried", "dead"}`` (used by tests/metrics).
    """
    from app.integrations.ushauth_client import is_retryable_upstream_error

    settings = get_settings()
    stats = {"delivered": 0, "retried": 0, "dead": 0}
    blocked_bookings: set[str] = set()
    rows = await _claim_due_batch(batch_size or settings.CACHE_OUTBOX_BATCH_SIZE)

    for row in rows:
        if row.booking_id in blocked_bookings:
            # An earlier row of this booking failed in this batch: keep order, retry next tick.
            await _finish(row.id, next_attempt_at=_utcnow() + timedelta(seconds=backoff_seconds(1)))
            continue
        try:
            await client._post(row.path, json_data=row.payload)
        except Exception as exc:
            attempts = (row.attempts or 0) + 1
            retryable = is_retryable_upstream_error(exc) or isinstance(exc, (httpx.HTTPError, OSError))
            if not retryable or attempts >= (row.max_attempts or settings.CACHE_OUTBOX_MAX_ATTEMPTS):
                await _finish(row.id, status=DEAD, attempts=attempts, last_error=str(exc)[:1000])
                logger.error(
                    "cache_outbox_dead",
                    booking_id=row.booking_id,
                    path=row.path,
                    attempts=attempts,
                    error=str(exc),
                )
                stats["dead"] += 1
                # A permanently failed row no longer blocks later rows of the booking.
            else:
                await _finish(
                    row.id,
                    attempts=attempts,
                    last_error=str(exc)[:1000],
                    next_attempt_at=_utcnow() + timedelta(seconds=backoff_seconds(attempts)),
                )
                blocked_bookings.add(row.booking_id)
                stats["retried"] += 1
            continue
        await _finish(row.id, status=DELIVERED, delivered_at=_utcnow(), last_error=None)
        stats["delivered"] += 1
        logger.info("cache_outbox_delivered", booking_id=row.booking_id, path=row.path)
    return stats


async def run_relay_forever(client_factory: Any) -> None:
    """Background loop started from the FastAPI lifespan."""
    settings = get_settings()
    logger.info("cache_outbox_relay_started", poll_seconds=settings.CACHE_OUTBOX_POLL_SECONDS)
    while True:
        try:
            await process_outbox_once(client_factory())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("cache_outbox_relay_error", error=str(exc))
        await asyncio.sleep(settings.CACHE_OUTBOX_POLL_SECONDS)
