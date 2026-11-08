"""
Postgres concurrency test: two concurrent reservations for the LAST session →
exactly one succeeds (row lock + DB check constraint).

Skipped unless ``TEST_DATABASE_URL`` points to a disposable PostgreSQL database
(e.g. ``postgresql+asyncpg://postgres:postgres@localhost:5432/ushbooknpay_test``).
Only the two course tables are created / dropped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (needs a real PostgreSQL)"),
]


@pytest.mark.asyncio
async def test_two_concurrent_reservations_for_last_session_only_one_wins():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.course.application.services import CourseSubscriptionService
    from app.course.domain.value_objects import CourseError
    from app.course.infrastructure.models import CourseSessionUsage, CourseSubscription
    from app.events.outbox.models import OutboxEvent

    engine = create_async_engine(TEST_DATABASE_URL)
    tables = [CourseSubscription.__table__, CourseSessionUsage.__table__, OutboxEvent.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: CourseSubscription.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    service_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    sub_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(
            CourseSubscription(
                id=sub_id,
                subscription_number=f"CRS/TEST/{uuid.uuid4().hex[:8]}",
                customer_id=customer_id,
                customer_data={},
                course_id=uuid.uuid4(),
                course_data={"name": "Test"},
                service_id=service_id,
                arrangement_type="vip_suite",
                total_sessions=2,
                used_sessions=1,
                reserved_sessions=0,
                validity_days=30,
                price=Decimal("10.000"),
                currency="KWD",
                status="active",
                activated_at=now,
                expires_at=now + timedelta(days=30),
                invoice_status="created",
                invoice_attempts=0,
            )
        )
        await s.commit()

    start = asyncio.Event()

    async def attempt() -> str:
        async with factory() as session:
            svc = CourseSubscriptionService(session)
            await start.wait()
            try:
                sub = await svc.lock_for_booking(
                    sub_id,
                    customer_id=customer_id,
                    services=[{"service_id": str(service_id)}],
                    appointment_start=now + timedelta(days=1),
                    arrangement_type="vip_suite",
                )
                await asyncio.sleep(0.2)  # hold the row lock while the other attempt waits
                booking = SimpleNamespace(
                    id=uuid.uuid4(), booking_number=None, customer_id=customer_id, branch_id=None, branch_data={},
                    therapist_id=None, therapist_data={}, service_arrangement_id=None, services=[],
                    service_id=service_id, service_data={}, service_arrangement_data={},
                    appointment_start=now + timedelta(days=1), appointment_end=now + timedelta(days=1, hours=1),
                )
                await svc.reserve(sub, booking)
                await session.commit()
                return "ok"
            except CourseError as exc:
                await session.rollback()
                return exc.code

    tasks = [asyncio.create_task(attempt()), asyncio.create_task(attempt())]
    await asyncio.sleep(0.05)
    start.set()
    results = sorted(await asyncio.gather(*tasks))

    async with factory() as s:
        sub = await s.get(CourseSubscription, sub_id)
        reserved = sub.reserved_sessions
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM course_session_usages WHERE subscription_id = %s" % f"'{sub_id}'")
        await conn.exec_driver_sql("DELETE FROM course_subscriptions WHERE id = %s" % f"'{sub_id}'")
    await engine.dispose()

    assert results == ["COURSE_NO_SESSIONS_AVAILABLE", "ok"]
    assert reserved == 1

