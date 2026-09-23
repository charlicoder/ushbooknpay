"""
tests/unit/voucher/test_voucher_validity.py
───────────────────────────────────────────
Unit tests verifying that when a new gift_voucher record is created:
1. Default validity is set to 60 days from creation time.
2. Custom validity_days or validity can be supplied.
3. Explicit expire_date takes precedence.
4. CreateGiftVoucherRequest schema validates validity_days and validity.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.infrastructure.models import _default_expire_date
from app.voucher.interfaces.schemas import CreateGiftVoucherRequest


def test_default_expire_date_is_60_days():
    """Verify _default_expire_date returns a datetime approx 60 days in future."""
    now = datetime.now(tz=timezone.utc)
    default_exp = _default_expire_date()

    delta = default_exp - now
    # Should be within 59 to 61 days (allowing for slight execution time)
    assert 59 <= delta.days <= 61


def test_create_voucher_request_default_validity_days():
    """Verify CreateGiftVoucherRequest defaults validity_days to 60."""
    req = CreateGiftVoucherRequest(
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
    )
    assert req.validity_days == 60
    assert req.validity is None
    assert req.expire_date is None


def test_create_voucher_request_custom_validity():
    """Verify CreateGiftVoucherRequest accepts custom validity or validity_days."""
    req = CreateGiftVoucherRequest(
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        validity=30,
    )
    assert req.validity == 30

    req2 = CreateGiftVoucherRequest(
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        validity_days=90,
    )
    assert req2.validity_days == 90


@pytest.mark.asyncio
async def test_create_voucher_service_default_validity_60_days():
    """Verify that when no expire_date is provided, create_voucher sets expire_date to ~60 days."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.add = MagicMock()
    svc._repo.flush = AsyncMock()
    svc._repo.generate_voucher_number = AsyncMock(return_value="V260922001")

    now_before = datetime.now(tz=timezone.utc)
    voucher = await svc.create_voucher(
        service_id=None,
        service_data=None,
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
    )
    now_after = datetime.now(tz=timezone.utc)

    assert voucher.expire_date is not None
    # Expire date should be ~60 days ahead of creation
    delta_days = (voucher.expire_date - now_before).days
    assert 59 <= delta_days <= 60
    assert voucher.expire_date >= now_before + timedelta(days=59)
    assert voucher.expire_date <= now_after + timedelta(days=61)


@pytest.mark.asyncio
async def test_create_voucher_service_custom_validity_days():
    """Verify create_voucher respects custom validity_days (e.g. 30 days)."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.add = MagicMock()
    svc._repo.flush = AsyncMock()
    svc._repo.generate_voucher_number = AsyncMock(return_value="V260922001")

    now = datetime.now(tz=timezone.utc)
    voucher = await svc.create_voucher(
        service_id=None,
        service_data=None,
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
        validity_days=30,
    )

    assert (voucher.expire_date - now).days in (29, 30)


@pytest.mark.asyncio
async def test_create_voucher_service_explicit_expire_date_precedence():
    """Verify explicit expire_date takes precedence over validity_days."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.add = MagicMock()
    svc._repo.flush = AsyncMock()
    svc._repo.generate_voucher_number = AsyncMock(return_value="V260922001")

    explicit_dt = datetime(2027, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    voucher = await svc.create_voucher(
        service_id=None,
        service_data=None,
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
        expire_date=explicit_dt,
        validity_days=30,
    )

    assert voucher.expire_date == explicit_dt
