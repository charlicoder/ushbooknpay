"""The double-booking check must agree with the appointment cache / schedule pages.

No-show, cancelled, refunded and payment-failed bookings release the slot (ushnotice deletes their
cache rows). Completed bookings keep their cache row and keep blocking, like every in-flight status.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.booking.domain.value_objects import NON_BLOCKING_BOOKING_STATUSES, BookingStatus
from app.booking.infrastructure.repository import BookingRepository

ACTIVE = [
    BookingStatus.DRAFT,
    BookingStatus.REQUESTED,
    BookingStatus.PAYMENT_PENDING,
    BookingStatus.CONFIRMED,
    BookingStatus.CHECKED_IN,
    BookingStatus.IN_PROGRESS,
    BookingStatus.RESCHEDULE_REQUESTED,
    BookingStatus.RESCHEDULE_APPROVED,
    BookingStatus.COMPLETED,
]
RELEASING = [
    BookingStatus.CANCELLED,
    BookingStatus.NO_SHOW,
    BookingStatus.REFUNDED,
    BookingStatus.PAYMENT_FAILED,
]


def test_status_partition_is_complete_and_disjoint():
    assert NON_BLOCKING_BOOKING_STATUSES == {s.value for s in RELEASING}
    assert not NON_BLOCKING_BOOKING_STATUSES & {s.value for s in ACTIVE}
    assert NON_BLOCKING_BOOKING_STATUSES | {s.value for s in ACTIVE} == {s.value for s in BookingStatus}


@pytest.mark.asyncio
async def test_check_therapist_overlap_excludes_releasing_statuses():
    import uuid
    from datetime import datetime, timezone

    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    repo = BookingRepository(session)

    now = datetime.now(timezone.utc)
    await repo.check_therapist_overlap(uuid.uuid4(), now, now)

    stmt = session.execute.await_args.args[0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    for status in RELEASING:
        assert f"'{status.value}'" in sql, f"{status.value} should be excluded from the overlap check"
    for status in ACTIVE:
        assert f"'{status.value}'" not in sql, f"{status.value} must still block"

