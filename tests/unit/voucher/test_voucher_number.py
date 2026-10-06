"""
tests/unit/voucher/test_voucher_number.py
──────────────────────────────────────────
Unit tests verifying the voucher_number generation:
Structure: "VOU" + "/" + YYYY + "/" + MM + "/" + [6 digit sequential number].
Example: For date 2026/10/06, the first voucher_number will be VOU/2026/10/000001.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.voucher.infrastructure.repository import GiftVoucherRepository


@pytest.mark.asyncio
async def test_generate_voucher_number_first_voucher_for_month():
    """First voucher for a given month starts at sequence 000001."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    repo = GiftVoucherRepository(session)
    voucher_num = await repo.generate_voucher_number(for_date=date(2026, 10, 6))

    assert voucher_num == "VOU/2026/10/000001"


@pytest.mark.asyncio
async def test_generate_voucher_number_increments_sequence():
    """Subsequent vouchers increment sequence based on existing count."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 1
    session.execute.return_value = mock_result

    repo = GiftVoucherRepository(session)
    voucher_num = await repo.generate_voucher_number(for_date=date(2026, 10, 6))

    assert voucher_num == "VOU/2026/10/000002"


@pytest.mark.asyncio
async def test_generate_voucher_number_higher_sequence():
    """Verify 6-digit zero padding with larger counts (e.g. 99 -> 000100)."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 99
    session.execute.return_value = mock_result

    repo = GiftVoucherRepository(session)
    voucher_num = await repo.generate_voucher_number(for_date=date(2026, 10, 15))

    assert voucher_num == "VOU/2026/10/000100"


@pytest.mark.asyncio
async def test_generate_voucher_number_defaults_to_today():
    """When for_date is omitted, defaults to today's date."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    repo = GiftVoucherRepository(session)
    voucher_num = await repo.generate_voucher_number()

    today_str = date.today().strftime("%Y/%m")
    assert voucher_num == f"VOU/{today_str}/000001"


@pytest.mark.asyncio
async def test_generate_voucher_number_handles_async_scalar():
    """Handles async mock return values where scalar_one is a coroutine."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_scalar = AsyncMock(return_value=12)
    mock_result.scalar_one = mock_scalar
    session.execute.return_value = mock_result

    repo = GiftVoucherRepository(session)
    voucher_num = await repo.generate_voucher_number(for_date=date(2026, 10, 6))

    assert voucher_num == "VOU/2026/10/000013"


@pytest.mark.asyncio
async def test_generate_voucher_number_handles_none_or_invalid():
    """Safely falls back to sequence 000001 if scalar_one returns None or non-integer."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = None
    session.execute.return_value = mock_result

    repo = GiftVoucherRepository(session)
    voucher_num = await repo.generate_voucher_number(for_date=date(2026, 10, 6))

    assert voucher_num == "VOU/2026/10/000001"
