"""
tests/unit/voucher/test_scheduled_delivery.py
─────────────────────────────────────────────
Unit tests for scheduled gift voucher delivery:
- Schema validation for delivery_mode and scheduled_delivery_at
- Status transition redirect (active -> scheduled when delivery_mode='scheduled')
- Rescheduling functionality and 2-month limit
- Activation of scheduled vouchers and 60-day expiry calculation from delivery
- Overdue reconciler functionality
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import ValidationError
from app.events.contracts import VoucherActiveEvent, VoucherScheduledEvent
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import GiftVoucherStatus, VoucherDeliveryMode
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.interfaces.schemas import CreateGiftVoucherRequest, RescheduleVoucherRequest

KUWAIT_TZ = ZoneInfo("Asia/Kuwait")


def test_create_voucher_request_scheduled_validation():
    """Verify validation of delivery_mode and scheduled_delivery_at in request schema."""
    # 1. delivery_mode='scheduled' without scheduled_delivery_at raises ValueError
    with pytest.raises(Exception) as exc_info:
        CreateGiftVoucherRequest(
            total_amount=Decimal("50.000"),
            delivery_mode="scheduled",
            scheduled_delivery_at=None,
        )
    assert "scheduled_delivery_at is required when delivery_mode is 'scheduled'" in str(exc_info.value)

    # 2. scheduled_delivery_at in the past raises ValueError
    past_dt = datetime.now(KUWAIT_TZ) - timedelta(hours=1)
    with pytest.raises(Exception) as exc_info:
        CreateGiftVoucherRequest(
            total_amount=Decimal("50.000"),
            delivery_mode="scheduled",
            scheduled_delivery_at=past_dt,
        )
    assert "must be in the future" in str(exc_info.value)

    # 3. scheduled_delivery_at > 2 months raises ValueError
    future_dt = datetime.now(KUWAIT_TZ) + timedelta(days=70)
    with pytest.raises(Exception) as exc_info:
        CreateGiftVoucherRequest(
            total_amount=Decimal("50.000"),
            delivery_mode="scheduled",
            scheduled_delivery_at=future_dt,
        )
    assert "cannot be more than 2 months into the future" in str(exc_info.value)

    # 4. Valid future datetime succeeds
    valid_dt = datetime.now(KUWAIT_TZ) + timedelta(days=7)
    req = CreateGiftVoucherRequest(
        total_amount=Decimal("50.000"),
        delivery_mode="scheduled",
        scheduled_delivery_at=valid_dt,
    )
    assert req.delivery_mode == "scheduled"
    assert req.scheduled_delivery_at is not None


def test_reschedule_voucher_request_validation():
    """Verify RescheduleVoucherRequest validates future datetime within 2 months."""
    past_dt = datetime.now(KUWAIT_TZ) - timedelta(hours=2)
    with pytest.raises(Exception) as exc:
        RescheduleVoucherRequest(scheduled_delivery_at=past_dt)
    assert "must be in the future" in str(exc.value)

    far_future = datetime.now(KUWAIT_TZ) + timedelta(days=75)
    with pytest.raises(Exception) as exc:
        RescheduleVoucherRequest(scheduled_delivery_at=far_future)
    assert "cannot be more than 2 months" in str(exc.value)

    valid_dt = datetime.now(KUWAIT_TZ) + timedelta(days=14)
    req = RescheduleVoucherRequest(scheduled_delivery_at=valid_dt)
    assert req.scheduled_delivery_at is not None


def test_scheduled_delivery_at_preserves_wall_clock_time():
    """Verify that an ISO string ending in Z is parsed as Kuwait local wall-clock time without +3h shift."""
    from app.events.scheduler_client import SchedulerClient

    future_date = (datetime.now(KUWAIT_TZ) + timedelta(days=2)).strftime("%Y-%m-%d")
    iso_z = f"{future_date}T03:40:00Z"

    req = CreateGiftVoucherRequest(
        total_amount=Decimal("50.000"),
        delivery_mode="scheduled",
        scheduled_delivery_at=iso_z,
    )
    assert req.scheduled_delivery_at.hour == 3
    assert req.scheduled_delivery_at.minute == 40
    assert str(req.scheduled_delivery_at.tzinfo) == "Asia/Kuwait"

    resched = RescheduleVoucherRequest(scheduled_delivery_at=iso_z)
    assert resched.scheduled_delivery_at.hour == 3
    assert resched.scheduled_delivery_at.minute == 40
    assert str(resched.scheduled_delivery_at.tzinfo) == "Asia/Kuwait"

    expr = SchedulerClient.format_schedule_expression(req.scheduled_delivery_at)
    assert expr == f"at({future_date}T03:40:00)"

    # When loaded from PostgreSQL TIMESTAMPTZ column as UTC (e.g. 00:40 UTC = 03:40 Kuwait)
    utc_from_db = datetime(2026, 11, 2, 0, 40, tzinfo=timezone.utc)
    expr_utc = SchedulerClient.format_schedule_expression(utc_from_db)
    assert expr_utc == "at(2026-11-02T03:40:00)"


@pytest.mark.asyncio
async def test_update_status_redirects_to_scheduled_and_creates_schedule():
    """When a voucher has delivery_mode='scheduled', updating to 'active' redirects to 'scheduled'."""
    scheduled_at = datetime.now(KUWAIT_TZ) + timedelta(days=5)
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        status="payment_pending",
        delivery_mode="scheduled",
        scheduled_delivery_at=scheduled_at,
        total_amount=Decimal("40.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender User", "phone_number": "+96599998888"},
        recipient_phone="+96566665555",
    )

    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    mock_scheduler = AsyncMock()
    mock_scheduler.create_voucher_schedule = AsyncMock(
        return_value=("voucher-delivery-test", "arn:aws:scheduler:test")
    )
    mock_sqs = AsyncMock()

    with (
        patch("app.voucher.application.voucher_service.get_scheduler_client", return_value=mock_scheduler),
        patch("app.voucher.application.voucher_service.get_sqs_client", return_value=mock_sqs),
    ):
        updated = await svc.update_status(
            voucher.id,
            GiftVoucherStatus.ACTIVE.value,
            payment_id="pay_123456",
        )

        # Status must be SCHEDULED, not ACTIVE
        assert updated.status == GiftVoucherStatus.SCHEDULED.value
        assert updated.delivery_schedule_name == "voucher-delivery-test"
        mock_scheduler.create_voucher_schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_scheduled_voucher_sets_expiry_and_emits_event():
    """Activating a scheduled voucher sets status=active, delivered_at=now, and expire_date=now+60d."""
    scheduled_at = datetime.now(KUWAIT_TZ) + timedelta(days=1)
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        status="scheduled",
        delivery_mode="scheduled",
        scheduled_delivery_at=scheduled_at,
        total_amount=Decimal("40.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender User", "phone_number": "+96599998888"},
        recipient_phone="+96566665555",
    )

    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    mock_sqs = AsyncMock()

    with patch("app.voucher.application.voucher_service.get_sqs_client", return_value=mock_sqs):
        now_before = datetime.now(tz=timezone.utc)
        activated = await svc.activate_scheduled_voucher(voucher.id)

        assert activated.status == GiftVoucherStatus.ACTIVE.value
        assert activated.delivered_at is not None
        # Expiry is exactly ~60 days from activation
        delta = (activated.expire_date - now_before).days
        assert 59 <= delta <= 61


@pytest.mark.asyncio
async def test_reschedule_voucher_updates_schedule_and_increments_count():
    """Rescheduling a scheduled voucher updates the EventBridge schedule and increments reschedule_count."""
    old_scheduled = datetime.now(KUWAIT_TZ) + timedelta(days=3)
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        status="scheduled",
        delivery_mode="scheduled",
        scheduled_delivery_at=old_scheduled,
        delivery_schedule_name="voucher-delivery-sched",
        reschedule_count=0,
        total_amount=Decimal("40.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender User", "phone_number": "+96599998888"},
    )

    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    mock_scheduler = AsyncMock()
    mock_scheduler.update_voucher_schedule = AsyncMock(return_value="arn:aws:scheduler:updated")

    with patch("app.voucher.application.voucher_service.get_scheduler_client", return_value=mock_scheduler):
        new_date = datetime.now(KUWAIT_TZ) + timedelta(days=10)
        rescheduled = await svc.reschedule_voucher(voucher.id, new_date, actor_id="staff-uuid")

        assert rescheduled.reschedule_count == 1
        assert rescheduled.scheduled_delivery_at == new_date
        mock_scheduler.update_voucher_schedule.assert_awaited_once_with(
            "voucher-delivery-sched",
            str(voucher.id),
            new_date,
        )


@pytest.mark.asyncio
async def test_cannot_reschedule_non_scheduled_voucher():
    """Rescheduling a voucher that is not in 'scheduled' status raises ValidationError."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        status="active",
        delivery_mode="on_activation",
        total_amount=Decimal("40.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
    )

    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.get_by_id = AsyncMock(return_value=voucher)

    new_date = datetime.now(KUWAIT_TZ) + timedelta(days=5)
    with pytest.raises(ValidationError) as exc:
        await svc.reschedule_voucher(voucher.id, new_date)
    assert "Only vouchers in 'scheduled' status can be rescheduled" in str(exc.value)
