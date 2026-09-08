"""
Unit tests for /my-vouchers/ endpoint, repository query, and phone formatting.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.requests import Request
from starlette.datastructures import URL

from app.core.security import TokenPayload
from app.main import app
from app.voucher.api.router import list_my_received_vouchers
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.infrastructure.repository import (
    GiftVoucherRepository,
    _parse_filter_date,
    get_phone_variants,
)


# ── Phone variant helpers ─────────────────────────────────────────────────────

def test_get_phone_variants():
    variants = get_phone_variants("+96541028985")
    assert "+96541028985" in variants
    assert "96541028985" in variants
    assert "41028985" in variants
    assert "0096541028985" in variants

    # 8-digit local Kuwait number
    local_variants = get_phone_variants("41028985")
    assert "41028985" in local_variants
    assert "+96541028985" in local_variants
    assert "96541028985" in local_variants

    # Empty / none
    assert get_phone_variants("") == []


def test_parse_filter_date():
    # YYYY-MM-DD start of day
    dt_start = _parse_filter_date("2026-09-04", end_of_day=False)
    assert dt_start == datetime(2026, 9, 4, 0, 0, 0, tzinfo=timezone.utc)

    # YYYY-MM-DD end of day
    dt_end = _parse_filter_date("2026-09-04", end_of_day=True)
    assert dt_end.year == 2026 and dt_end.month == 9 and dt_end.day == 4
    assert dt_end.hour == 23 and dt_end.minute == 59

    # ISO string
    dt_iso = _parse_filter_date("2026-09-04T12:30:00Z")
    assert dt_iso == datetime(2026, 9, 4, 12, 30, 0, tzinfo=timezone.utc)

    # Invalid
    assert _parse_filter_date("not-a-date") is None
    assert _parse_filter_date(None) is None


# ── Repository tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repository_list_by_recipient_phone():
    session = AsyncMock()

    # Mock count query
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 1

    # Mock items query
    v_id = uuid.uuid4()
    voucher = MagicMock(spec=GiftVoucher)
    voucher.id = v_id
    voucher.recipient_phone = "+96541028985"
    voucher.status = "active"
    voucher.created_at = datetime.now(timezone.utc)

    mock_rows_result = MagicMock()
    mock_rows_result.scalars.return_value.all.return_value = [voucher]

    session.execute.side_effect = [mock_count_result, mock_rows_result]

    repo = GiftVoucherRepository(session)
    items, total = await repo.list_by_recipient_phone(
        "+96541028985",
        status="active",
        created_at="2026-09-04",
        from_date="2026-09-01",
        to_date="2026-09-05",
        available_only=False,
        page=1,
        page_size=10,
    )

    assert total == 1
    assert len(items) == 1
    assert items[0].id == v_id
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_repository_list_by_recipient_phone_available_filter():
    session = AsyncMock()

    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 0

    mock_rows_result = MagicMock()
    mock_rows_result.scalars.return_value.all.return_value = []

    session.execute.side_effect = [mock_count_result, mock_rows_result]

    repo = GiftVoucherRepository(session)
    items, total = await repo.list_by_recipient_phone(
        "41028985",
        status="available",
        available_only=True,
    )

    assert total == 0
    assert len(items) == 0


# ── Service tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_list_vouchers_for_recipient():
    session = AsyncMock()
    svc = GiftVoucherService(session)

    with patch.object(svc._repo, "list_by_recipient_phone", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = ([], 0)

        items, total = await svc.list_vouchers_for_recipient(
            "+96541028985",
            status="active",
            created_at="2026-09-04",
        )

        assert total == 0
        mock_list.assert_awaited_once_with(
            "+96541028985",
            status="active",
            created_at="2026-09-04",
            from_date=None,
            to_date=None,
            service_id=None,
            branch_id=None,
            available_only=False,
            page=1,
            page_size=20,
        )


# ── Endpoint tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_my_received_vouchers_success():
    current_user = TokenPayload(
        sub=str(uuid.uuid4()),
        phone_number="+96541028985",
        first_name="Alayna",
    )
    session = AsyncMock()

    # Mock request
    request = MagicMock(spec=Request)
    request.url = URL("https://api.ushspa.com/api/v1/my-vouchers/?status=active")

    voucher_mock = MagicMock(spec=GiftVoucher)
    voucher_mock.id = uuid.uuid4()
    voucher_mock.service_id = uuid.uuid4()
    voucher_mock.service_data = {"name": "Signature Massage"}
    voucher_mock.branch_id = uuid.uuid4()
    voucher_mock.branch_data = {"name": "Main Branch"}
    voucher_mock.service_arrangement_id = None
    voucher_mock.service_arrangement_data = {}
    voucher_mock.addons = []
    voucher_mock.extra_time = 0
    voucher_mock.price_for_extra_time = None
    voucher_mock.expire_date = datetime.now(timezone.utc)
    voucher_mock.status = "active"
    voucher_mock.sender_id = uuid.uuid4()
    voucher_mock.sender_data = {"name": "Alice"}
    voucher_mock.recipient_phone = "+96541028985"
    voucher_mock.recipient_id = None
    voucher_mock.recipient_data = {"name": "Alayna"}
    voucher_mock.created_by = None
    voucher_mock.total_duration = 60
    voucher_mock.total_amount = Decimal("45.000")
    voucher_mock.currency = "KWD"
    voucher_mock.gift_message = "Happy Birthday!"
    voucher_mock.gift_template = "template_1"
    voucher_mock.secret_code = "123456"
    voucher_mock.public_token = "pubtok123"
    voucher_mock.redeemed_booking_id = None
    voucher_mock.redeemed_at = None
    voucher_mock.booking_id = None
    voucher_mock.booking_data = {}
    voucher_mock.payment_id = "100624710000000255"
    voucher_mock.payment_data = None
    voucher_mock.payment_url = None
    voucher_mock.created_at = datetime.now(timezone.utc)
    voucher_mock.updated_at = datetime.now(timezone.utc)

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_vouchers_for_recipient",
        new_callable=AsyncMock,
    ) as mock_list:
        mock_list.return_value = ([voucher_mock], 1)

        resp = await list_my_received_vouchers(
            current_user=current_user,
            session=session,
            request=request,
            status_filter="active",
        )

        assert resp.status_code == 200
        import json
        body = json.loads(resp.body)
        assert body["success"] is True
        assert body["meta"]["pagination"]["count"] == 1
        assert len(body["data"]) == 1
        assert body["data"][0]["recipient_phone"] == "+96541028985"
        assert body["data"][0]["secret_code"] == "123456"
        assert body["data"][0]["gift_message"] == "Happy Birthday!"


@pytest.mark.asyncio
async def test_list_my_received_vouchers_missing_phone_raises_400():
    current_user = TokenPayload(
        sub=str(uuid.uuid4()),
        phone_number=None,
    )
    session = AsyncMock()
    request = MagicMock(spec=Request)
    request.url = URL("https://api.ushspa.com/api/v1/my-vouchers/")

    with pytest.raises(HTTPException) as exc_info:
        await list_my_received_vouchers(
            current_user=current_user,
            session=session,
            request=request,
            phone=None,
        )

    assert exc_info.value.status_code == 400


def test_routes_registered_on_fastapi():
    """Verify that /api/v1/my-vouchers/ and /api/v1/vouchers/my-vouchers/ are both registered."""
    paths = [route.path for route in app.routes]
    assert "/api/v1/my-vouchers/" in paths
    assert "/api/v1/vouchers/my-vouchers/" in paths
