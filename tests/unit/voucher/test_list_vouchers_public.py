"""
tests/unit/voucher/test_list_vouchers_public.py
──────────────────────────────────────────────
Unit tests for public /api/v1/vouchers/ and /booknpay/api/v1/vouchers/ endpoints.
Verifies:
- Only USH_TOKEN is required (no customer JWT needed)
- All vouchers are returned without filtering by created_by/sender_id
- Multiple header formats (USH_TOKEN, USH-TOKEN, X-USHSPA-TOKEN) and query param supported
- Routes are registered under both /api/v1/vouchers/ and /booknpay/api/v1/vouchers/
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import Headers, URL

from app.core.config import Settings
from app.core.security import _get_ushspa_token, verify_ushspa_token
from app.main import app
from app.voucher.api.router import list_all_vouchers
from app.voucher.infrastructure.models import GiftVoucher


def _make_mock_voucher(
    voucher_id: uuid.UUID | None = None,
    sender_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    status: str = "active",
) -> GiftVoucher:
    v = MagicMock(spec=GiftVoucher)
    v.id = voucher_id or uuid.uuid4()
    v.service_id = uuid.uuid4()
    v.service_data = {"name": "Signature Massage"}
    v.branch_id = uuid.uuid4()
    v.branch_data = {"name": "Salmiya Spa"}
    v.service_arrangement_id = uuid.uuid4()
    v.service_arrangement_data = {"name": "VIP Suite"}
    v.addons = [{"name": "Aromatherapy", "price": "7.000"}]
    v.extra_time = 15
    v.price_for_extra_time = None
    v.expire_date = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    v.status = status
    v.sender_id = sender_id or uuid.uuid4()
    v.sender_data = {"name": "Alice", "phone": "+96511111111"}
    v.recipient_phone = "+96522222222"
    v.recipient_id = None
    v.recipient_data = {"name": "Bob", "phone": "+96522222222"}
    v.created_by = created_by or uuid.uuid4()
    v.total_duration = 75
    v.total_amount = Decimal("50.000")
    v.currency = "KWD"
    v.gift_message = "Enjoy your treatment!"
    v.gift_template = "luxury_gold"
    v.secret_code = "SEC-9988"
    v.public_token = "pub-token-123"
    v.redeemed_booking_id = None
    v.redeemed_at = None
    v.booking_id = None
    v.booking_data = {}
    v.payment_id = "PAY-12345"
    v.payment_data = {"status": "paid"}
    v.payment_url = "https://checkout.example.com/12345"
    v.created_at = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    v.updated_at = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    return v


# ── Route Registration Tests ──────────────────────────────────────────────────

def test_vouchers_routes_registered():
    """Verify that both /api/v1/vouchers/ and /booknpay/api/v1/vouchers/ are registered."""
    paths = [route.path for route in app.routes]
    assert "/api/v1/vouchers/" in paths
    assert "/booknpay/api/v1/vouchers/" in paths


# ── USH_TOKEN Extraction & Verification Tests ─────────────────────────────────

def test_get_ush_token_from_ush_token_header():
    req = MagicMock(spec=Request)
    req.headers = Headers({"USH_TOKEN": "test-token-123"})
    req.query_params = {}
    assert _get_ushspa_token(req) == "test-token-123"


def test_get_ush_token_from_ush_dash_token_header():
    req = MagicMock(spec=Request)
    req.headers = Headers({"USH-TOKEN": "test-token-123"})
    req.query_params = {}
    assert _get_ushspa_token(req) == "test-token-123"


def test_get_ush_token_from_x_ush_token_header():
    req = MagicMock(spec=Request)
    req.headers = Headers({"X-USH-TOKEN": "test-token-123"})
    req.query_params = {}
    assert _get_ushspa_token(req) == "test-token-123"


def test_get_ush_token_from_x_ushspa_token_header():
    req = MagicMock(spec=Request)
    req.headers = Headers({"X-USHSPA-TOKEN": "test-token-123"})
    req.query_params = {}
    assert _get_ushspa_token(req) == "test-token-123"


def test_get_ush_token_from_query_param():
    req = MagicMock(spec=Request)
    req.headers = Headers({})
    req.query_params = {"ush_token": "test-token-query"}
    assert _get_ushspa_token(req) == "test-token-query"


def test_get_ush_token_missing_raises_401():
    req = MagicMock(spec=Request)
    req.headers = Headers({})
    req.query_params = {}
    with pytest.raises(HTTPException) as exc_info:
        _get_ushspa_token(req)
    assert exc_info.value.status_code == 401


def test_verify_ush_token_matching():
    settings = MagicMock(spec=Settings)
    settings.USHSPA_TOKEN = "secret123"
    settings.USH_TOKEN = "secret123"
    # Should not raise
    verify_ushspa_token("secret123", settings)


def test_verify_ush_token_mismatch_raises():
    from app.core.exceptions import AuthorizationError
    settings = MagicMock(spec=Settings)
    settings.USHSPA_TOKEN = "secret123"
    settings.USH_TOKEN = "secret123"
    with pytest.raises(AuthorizationError):
        verify_ushspa_token("wrong-token", settings)


# ── Endpoint Behavior Tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_all_vouchers_sends_all_vouchers_without_filter():
    """Verify list_all_vouchers calls list_all without sender filtering and returns vouchers."""
    session = AsyncMock()
    v1 = _make_mock_voucher(created_by=uuid.uuid4(), sender_id=uuid.uuid4())
    v2 = _make_mock_voucher(created_by=uuid.uuid4(), sender_id=uuid.uuid4())

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_all",
        new_callable=AsyncMock,
    ) as mock_list_all:
        mock_list_all.return_value = ([v1, v2], 2)

        resp = await list_all_vouchers(
            _=None,
            session=session,
            status_filter=None,
            sender_id=None,
            service_id=None,
        )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert len(body["data"]) == 2
        assert body["meta"]["pagination"]["count"] == 2

        # Verify list_all was called with sender_id=None (no filtering by created_by)
        mock_list_all.assert_awaited_once_with(
            status=None,
            sender_id=None,
            service_id=None,
            page=1,
            page_size=1000,
        )

        # Check fields in returned voucher
        item = body["data"][0]
        assert "created_by" in item
        assert "secret_code" in item
        assert "total_amount" in item
        assert item["secret_code"] == "SEC-9988"
        assert item["total_amount"] == "50.000"


@pytest.mark.asyncio
async def test_list_all_vouchers_with_status_filter():
    """Verify list_all_vouchers respects optional status filtering."""
    session = AsyncMock()
    v1 = _make_mock_voucher(status="redeemed")

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_all",
        new_callable=AsyncMock,
    ) as mock_list_all:
        mock_list_all.return_value = ([v1], 1)

        resp = await list_all_vouchers(
            _=None,
            session=session,
            status_filter="redeemed",
            sender_id=None,
            service_id=None,
            page=2,
            page_size=10,
        )

        assert resp.status_code == 200
        mock_list_all.assert_awaited_once_with(
            status="redeemed",
            sender_id=None,
            service_id=None,
            page=2,
            page_size=10,
        )
