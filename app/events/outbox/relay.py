"""
app/events/outbox/relay.py
──────────────────────────
Publishes ``outbox_events`` rows to SQS.

* :func:`publish_outbox_ids` – called right after a commit for the rows written
  in that transaction (see ``writer.py``).
* :func:`process_outbox_once` / :func:`run_outbox_relay_forever` – background
  relay started in the FastAPI lifespan: retries pending rows with exponential
  backoff and dead-letters them (``status='failed'``) after ``max_retries``
  attempts (``OUTBOX_MAX_ATTEMPTS``, default 5).

Rows are claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` so several replicas
(and the after-commit publisher) never send the same row twice concurrently.
Delivery is at-least-once; consumers dedupe on ``event_id``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.events.outbox.models import OutboxEvent

logger = get_logger(__name__)

PENDING = "pending"
PUBLISHED = "published"
FAILED = "failed"  # dead-letter (terminal)

_MAX_BACKOFF_SECONDS = 15 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff: 5s, 10s, 20s, 40s … capped at 15 minutes."""
    return min(5 * (2 ** max(attempts - 1, 0)), _MAX_BACKOFF_SECONDS)


async def _send(row: OutboxEvent, sqs: Any) -> None:
    """Send one row; raises on transport failure (unlike ``SQSClient.publish_event``)."""
    settings = get_settings()
    queue = row.queue_url or settings.AWS_SQS_NOTIFICATION_QUEUE_URL
    if not queue:
        logger.warning("outbox_no_queue_configured_event_not_sent", event_name=row.event_name, outbox_id=str(row.id))
        return
    payload = row.payload or {}
    await sqs.send_message(
        queue_url=queue,
        message_body=json.dumps(payload, default=str),
        message_group_id=row.message_group_id,
        message_attributes={
            "event_name": str(payload.get("event_name") or row.event_name),
            "event_type": str(payload.get("event_type") or ""),
            "event_id": str(payload.get("event_id") or row.id),
        },
    )


def apply_delivery_result(row: OutboxEvent, error: Exception | None, now: datetime | None = None) -> str:
    """Update ``row`` after a delivery attempt. Returns ``published`` | ``retried`` | ``dead``."""
    now = now or _utcnow()
    if error is None:
        row.status = PUBLISHED
        row.published_at = now
        row.last_error = None
        return "published"
    row.retry_count = int(row.retry_count or 0) + 1
    row.last_error = str(error)[:2000]
    max_retries = int(row.max_retries or get_settings().OUTBOX_MAX_ATTEMPTS)
    if row.retry_count >= max_retries:
        row.status = FAILED
        logger.error(
            "outbox_event_dead_lettered",
            outbox_id=str(row.id),
            event_name=row.event_name,
            attempts=row.retry_count,
            error=str(error),
        )
        return "dead"
    row.scheduled_at = now + timedelta(seconds=backoff_seconds(row.retry_count))
    return "retried"


async def _deliver_rows(session: Any, rows: list[OutboxEvent]) -> dict[str, int]:
    from app.events.sqs_client import get_sqs_client

    sqs = get_sqs_client()
    stats = {"published": 0, "retried": 0, "dead": 0}
    for row in rows:
        err: Exception | None = None
        try:
            await _send(row, sqs)
        except Exception as exc:
            err = exc
        stats[apply_delivery_result(row, err)] += 1
    await session.commit()
    return stats


async def publish_outbox_ids(ids: list[uuid.UUID]) -> dict[str, int]:
    """Publish the given (just committed) rows if still pending. Never raises."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(OutboxEvent)
                        .where(OutboxEvent.id.in_(ids), OutboxEvent.status == PENDING)
                        .order_by(OutboxEvent.created_at)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
            )
            if not rows:
                return {"published": 0, "retried": 0, "dead": 0}
            return await _deliver_rows(session, rows)
    except Exception as exc:
        logger.warning("outbox_after_commit_publish_failed", error=str(exc))
        return {"published": 0, "retried": 0, "dead": 0}


async def process_outbox_once(batch_size: int | None = None, session_factory: Any = None) -> dict[str, int]:
    """Deliver one batch of due pending rows. Returns counters (tests / metrics)."""
    settings = get_settings()
    factory = session_factory or get_session_factory()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.status == PENDING, OutboxEvent.scheduled_at <= _utcnow())
                    .order_by(OutboxEvent.created_at)
                    .limit(batch_size or settings.OUTBOX_RELAY_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
        )
        if not rows:
            return {"published": 0, "retried": 0, "dead": 0}
        return await _deliver_rows(session, rows)


async def run_outbox_relay_forever() -> None:
    """Background loop started from the FastAPI lifespan."""
    settings = get_settings()
    logger.info("outbox_relay_started", poll_seconds=settings.OUTBOX_RELAY_POLL_SECONDS)
    while True:
        try:
            await process_outbox_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("outbox_relay_error", error=str(exc))
        await asyncio.sleep(settings.OUTBOX_RELAY_POLL_SECONDS)

