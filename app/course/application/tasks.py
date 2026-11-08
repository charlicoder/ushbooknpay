"""
app/course/application/tasks.py
───────────────────────────────
Background loops started from the FastAPI lifespan (``COURSE_TASKS_ENABLED``):

* expiry   – every ``COURSE_EXPIRY_TASK_INTERVAL_SECONDS`` (default 1h) active
             subscriptions past ``expires_at`` become ``expired``.
* invoices – every ``COURSE_INVOICE_RETRY_INTERVAL_SECONDS`` paid subscriptions
             whose ushanr invoice failed are retried (idempotent on source).

Each iteration runs in its own transaction; rows are claimed with
``FOR UPDATE SKIP LOCKED`` so several replicas can run the loops safely.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger

logger = get_logger(__name__)


async def run_expiry_once(session_factory: Any = None) -> int:
    from app.course.application.services import CourseSubscriptionService

    factory = session_factory or get_session_factory()
    async with factory() as session:
        count = await CourseSubscriptionService(session).expire_due()
        await session.commit()
    if count:
        logger.info("course_subscriptions_expired", count=count)
    return count


async def run_invoice_retry_once(session_factory: Any = None) -> int:
    from app.course.application.services import CourseSubscriptionService

    factory = session_factory or get_session_factory()
    async with factory() as session:
        count = await CourseSubscriptionService(session).retry_failed_invoices()
        await session.commit()
    if count:
        logger.info("course_invoices_retried", created=count)
    return count


async def _loop(name: str, fn: Any, interval: int) -> None:
    logger.info("course_task_started", task=name, interval_seconds=interval)
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("course_task_error", task=name, error=str(exc))
        await asyncio.sleep(interval)


async def run_expiry_forever() -> None:
    await _loop("expiry", run_expiry_once, get_settings().COURSE_EXPIRY_TASK_INTERVAL_SECONDS)


async def run_invoice_retry_forever() -> None:
    await _loop("invoice_retry", run_invoice_retry_once, get_settings().COURSE_INVOICE_RETRY_INTERVAL_SECONDS)

