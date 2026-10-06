"""
tests/unit/booking/test_booking_number.py
──────────────────────────────────────────
Unit tests verifying the booking_number generation:
Structure: "BOK" + "/" + YYYY + "/" + MM + "/" + [6 digit sequential number].
Example: For date 2026/10/06, the first booking_number will be BOK/2026/10/000001.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.booking.infrastructure.repository import BookingRepository


@pytest.mark.asyncio
async def test_generate_booking_number_first_booking_for_month():
    """First booking for a given month starts at sequence 000001."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    repo = BookingRepository(session)
    booking_num = await repo.generate_booking_number(for_date=date(2026, 10, 6))

    assert booking_num == "BOK/2026/10/000001"


@pytest.mark.asyncio
async def test_generate_booking_number_increments_sequence():
    """Subsequent bookings increment sequence based on existing count."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 1
    session.execute.return_value = mock_result

    repo = BookingRepository(session)
    booking_num = await repo.generate_booking_number(for_date=date(2026, 10, 6))

    assert booking_num == "BOK/2026/10/000002"


@pytest.mark.asyncio
async def test_generate_booking_number_higher_sequence():
    """Verify 6-digit zero padding with larger counts (e.g. 150 -> 000151)."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 150
    session.execute.return_value = mock_result

    repo = BookingRepository(session)
    booking_num = await repo.generate_booking_number(for_date=date(2026, 10, 15))

    assert booking_num == "BOK/2026/10/000151"


@pytest.mark.asyncio
async def test_generate_booking_number_defaults_to_today():
    """When for_date is omitted, defaults to today's date."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    repo = BookingRepository(session)
    booking_num = await repo.generate_booking_number()

    today_str = date.today().strftime("%Y/%m")
    assert booking_num == f"BOK/{today_str}/000001"


@pytest.mark.asyncio
async def test_generate_booking_number_handles_async_scalar():
    """Handles async mock return values where scalar_one is a coroutine."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_scalar = AsyncMock(return_value=9)
    mock_result.scalar_one = mock_scalar
    session.execute.return_value = mock_result

    repo = BookingRepository(session)
    booking_num = await repo.generate_booking_number(for_date=date(2026, 10, 6))

    assert booking_num == "BOK/2026/10/000010"


@pytest.mark.asyncio
async def test_generate_booking_number_handles_none_or_invalid():
    """Safely falls back to sequence 000001 if scalar_one returns None or non-integer."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = None
    session.execute.return_value = mock_result

    repo = BookingRepository(session)
    booking_num = await repo.generate_booking_number(for_date=date(2026, 10, 6))

    assert booking_num == "BOK/2026/10/000001"
