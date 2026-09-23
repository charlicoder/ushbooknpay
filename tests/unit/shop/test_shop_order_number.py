"""
tests/unit/shop/test_shop_order_number.py
──────────────────────────────────────────
Unit tests verifying the shop order_number generation:
Structure: "ORD-" + "YY" + "MM" + "DD" + 3-digit auto increment from 001 to 999.
Example: For date 2026/09/21, the first order_number will be ORD-260921001.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.shop.infrastructure.repository import ShopOrderRepository


@pytest.mark.asyncio
async def test_next_order_number_first_order_for_date():
    """First order for a given date starts at sequence 001."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    repo = ShopOrderRepository(session)
    order_num = await repo.next_order_number(for_date=date(2026, 9, 21))

    assert order_num == "ORD-260921001"


@pytest.mark.asyncio
async def test_next_order_number_increments_sequence():
    """Subsequent orders increment sequence based on existing count."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 1
    session.execute.return_value = mock_result

    repo = ShopOrderRepository(session)
    order_num = await repo.next_order_number(for_date=date(2026, 9, 21))

    assert order_num == "ORD-260921002"


@pytest.mark.asyncio
async def test_next_order_number_higher_sequence():
    """Verify 3-digit zero padding with larger counts (e.g. 42 -> 043)."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 42
    session.execute.return_value = mock_result

    repo = ShopOrderRepository(session)
    order_num = await repo.next_order_number(for_date=date(2026, 12, 5))

    assert order_num == "ORD-261205043"


@pytest.mark.asyncio
async def test_next_order_number_defaults_to_today():
    """When for_date is omitted, defaults to today's date."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    repo = ShopOrderRepository(session)
    order_num = await repo.next_order_number()

    today_str = date.today().strftime("%y%m%d")
    assert order_num == f"ORD-{today_str}001"


@pytest.mark.asyncio
async def test_next_order_number_handles_async_scalar_coroutine():
    """Handles async mock return values where scalar_one is a coroutine."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_scalar = AsyncMock(return_value=5)
    mock_result.scalar_one = mock_scalar
    session.execute.return_value = mock_result

    repo = ShopOrderRepository(session)
    order_num = await repo.next_order_number(for_date=date(2026, 9, 21))

    assert order_num == "ORD-260921006"


@pytest.mark.asyncio
async def test_next_order_number_handles_none_or_invalid_scalar():
    """Safely falls back to sequence 001 if scalar_one returns None or non-integer."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = None
    session.execute.return_value = mock_result

    repo = ShopOrderRepository(session)
    order_num = await repo.next_order_number(for_date=date(2026, 9, 21))

    assert order_num == "ORD-260921001"
