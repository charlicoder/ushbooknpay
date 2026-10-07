"""
tests/unit/payment/test_payment_number.py
──────────────────────────────────────────
Unit tests verifying the payment_number generation and API handling:
Structure: "PMT" + "/" + YYYY + "/" + MM + "/" + [6 digit sequential number].
Example: For date 2026/10/06, the first payment_number will be PMT/2026/10/000001.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.payments import (
    _payment_to_detail,
    _payment_to_list_item,
    create_payment,
)
from app.payment.infrastructure.models import Payment
from app.payment.infrastructure.repository import (
    PaymentRepository,
    generate_payment_number,
)
from app.payment.interfaces.schemas import (
    CreatePaymentRequestSchema,
    PaymentDetailResponse,
    PaymentListItem,
    PaymentSessionResponse,
    PaymentStatusResponse,
    UpdatePaymentRequestSchema,
)


@pytest.mark.asyncio
async def test_generate_payment_number_first_payment_for_month():
    """First payment for a given month starts at sequence 000001."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    payment_num = await generate_payment_number(session, for_date=date(2026, 10, 6))

    assert payment_num == "PMT/2026/10/000001"


@pytest.mark.asyncio
async def test_generate_payment_number_increments_sequence():
    """Subsequent payments increment sequence based on existing count."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 1
    session.execute.return_value = mock_result

    payment_num = await generate_payment_number(session, for_date=date(2026, 10, 6))

    assert payment_num == "PMT/2026/10/000002"


@pytest.mark.asyncio
async def test_generate_payment_number_higher_sequence():
    """Verify 6-digit zero padding with larger counts (e.g. 99 -> 000100)."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 99
    session.execute.return_value = mock_result

    payment_num = await generate_payment_number(session, for_date=date(2026, 10, 15))

    assert payment_num == "PMT/2026/10/000100"


@pytest.mark.asyncio
async def test_generate_payment_number_defaults_to_today():
    """When for_date is omitted, defaults to today's date."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    session.execute.return_value = mock_result

    payment_num = await generate_payment_number(session)

    today_str = date.today().strftime("%Y/%m")
    assert payment_num == f"PMT/{today_str}/000001"


@pytest.mark.asyncio
async def test_generate_payment_number_handles_async_scalar():
    """Handles async mock return values where scalar_one is a coroutine."""
    session = AsyncMock()
    mock_result = MagicMock()

    async def _async_scalar():
        return 4

    mock_result.scalar_one = _async_scalar
    session.execute.return_value = mock_result

    payment_num = await generate_payment_number(session, for_date=date(2026, 10, 7))
    assert payment_num == "PMT/2026/10/000005"


@pytest.mark.asyncio
async def test_generate_payment_number_handles_exceptions():
    """Falls back to 000001 if scalar_one raises ValueError or TypeError."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.side_effect = TypeError("Bad count")
    session.execute.return_value = mock_result

    payment_num = await generate_payment_number(session, for_date=date(2026, 10, 7))
    assert payment_num == "PMT/2026/10/000001"


@pytest.mark.asyncio
async def test_payment_repository_generate_payment_number():
    """PaymentRepository delegates to generate_payment_number."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 2
    session.execute.return_value = mock_result

    repo = PaymentRepository(session)
    payment_num = await repo.generate_payment_number(for_date=date(2026, 10, 1))
    assert payment_num == "PMT/2026/10/000003"


def test_payment_model_attributes_and_repr():
    """Payment model accepts and displays payment_number."""
    p = Payment(
        payment_number="PMT/2026/10/000001",
        customer_id=uuid.uuid4(),
        total_amount=Decimal("50.000"),
        total_duration=60,
        currency="KWD",
    )
    assert p.payment_number == "PMT/2026/10/000001"
    assert "PMT/2026/10/000001" in repr(p)


def test_payment_schemas_include_payment_number():
    """PaymentListItem, PaymentDetailResponse, PaymentStatusResponse include payment_number."""
    item = PaymentListItem(
        id=str(uuid.uuid4()),
        payment_number="PMT/2026/10/000001",
        customer_id=str(uuid.uuid4()),
        total_amount="25.000",
        total_duration=60,
        currency="KWD",
        created_at=datetime.now(timezone.utc),
    )
    assert item.payment_number == "PMT/2026/10/000001"

    detail = PaymentDetailResponse(
        id=str(uuid.uuid4()),
        payment_number="PMT/2026/10/000001",
        customer_id=str(uuid.uuid4()),
        total_amount="25.000",
        total_duration=60,
        currency="KWD",
        created_at=datetime.now(timezone.utc),
    )
    assert detail.payment_number == "PMT/2026/10/000001"

    status_resp = PaymentStatusResponse(
        payment_id=str(uuid.uuid4()),
        payment_number="PMT/2026/10/000001",
        payment_provider="MyFatoorah",
        total_amount="25.000",
        currency="KWD",
        created_at=datetime.now(timezone.utc),
    )
    assert status_resp.payment_number == "PMT/2026/10/000001"


def test_payment_mappers_map_payment_number():
    """_payment_to_detail and _payment_to_list_item correctly map payment_number."""
    p = Payment(
        id=uuid.uuid4(),
        payment_number="PMT/2026/10/000007",
        customer_id=uuid.uuid4(),
        total_amount=Decimal("30.000"),
        total_duration=45,
        currency="KWD",
        created_at=datetime.now(timezone.utc),
    )
    detail = _payment_to_detail(p)
    assert detail.payment_number == "PMT/2026/10/000007"

    list_item = _payment_to_list_item(p)
    assert list_item.payment_number == "PMT/2026/10/000007"


@pytest.mark.asyncio
async def test_create_payment_auto_generates_payment_number():
    """POST /payments/ auto-generates payment_number when not provided."""
    session = AsyncMock()
    count_mock = MagicMock()
    count_mock.scalar_one.return_value = 0
    session.execute.return_value = count_mock

    body = CreatePaymentRequestSchema(
        customer_id=uuid.uuid4(),
        total_amount=Decimal("15.000"),
        total_duration=30,
        currency="KWD",
    )
    token = {"sub": "user-test-1"}
    settings = MagicMock()

    resp = await create_payment(body, token, session, settings)
    assert resp.status_code == 201

    # Check that payment was added with an auto-generated payment_number
    added_payment = session.add.call_args_list[0][0][0]
    assert added_payment.payment_number.startswith("PMT/")
    assert added_payment.payment_number.endswith("/000001")
