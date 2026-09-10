"""
tests/unit/voucher/test_create_voucher_recipient_resolution.py
──────────────────────────────────────────────────────────────
Tests that POST /api/v1/vouchers/ resolves recipient_id and recipient_data
by calling ushauth get-or-create before saving the voucher.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.security import TokenPayload
from app.voucher.api.router import create_gift_voucher
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.interfaces.schemas import CreateGiftVoucherRequest

# ── Shared fixtures ───────────────────────────────────────────────────────────

SENDER_ID = str(uuid.uuid4())
RECIPIENT_ID = str(uuid.uuid4())
SERVICE_ID = str(uuid.uuid4())

USHAUTH_PROFILE = {
    "id": RECIPIENT_ID,
    "name": "Alice Smith",
    "phone_number": "+96541028983",
    "email": "alice@example.com",
    "avatar": None,
    "created": False,
}

MOCK_USER = TokenPayload(
    sub=SENDER_ID,
    first_name="Bob",
    last_name="",
    phone_number="+96541028000",
)

VALID_REQUEST = CreateGiftVoucherRequest(
    service_id=uuid.UUID(SERVICE_ID),
    service_data={"name": "Hot Stone Massage"},
    total_amount=Decimal("45.000"),
    currency="KWD",
    total_duration=60,
    recipient_phone="+96541028983",
    recipient_data={"name": "Alice Smith"},
    sender_data={"name": "Bob", "phone_number": "+96541028000"},
)


def _make_mock_voucher() -> MagicMock:
    v = MagicMock(spec=GiftVoucher)
    v.id = uuid.uuid4()
    v.service_id = uuid.UUID(SERVICE_ID)
    v.service_data = {"name": "Hot Stone Massage"}
    v.branch_id = None
    v.branch_data = {}
    v.service_arrangement_id = None
    v.service_arrangement_data = {}
    v.addons = []
    v.extra_time = 0
    v.price_for_extra_time = None
    v.expire_date = datetime(2026, 12, 31, tzinfo=timezone.utc)
    v.status = "created"
    v.sender_id = uuid.UUID(SENDER_ID)
    v.sender_data = {"name": "Bob"}
    v.recipient_phone = "+96541028983"
    v.recipient_id = uuid.UUID(RECIPIENT_ID)
    v.recipient_data = {"name": "Alice Smith", "phone_number": "+96541028983"}
    v.created_by = uuid.UUID(SENDER_ID)
    v.total_duration = 60
    v.total_amount = Decimal("45.000")
    v.currency = "KWD"
    v.gift_message = None
    v.gift_template = None
    v.secret_code = "SEC123"
    v.public_token = "tok_xyz"
    v.redeemed_booking_id = None
    v.redeemed_at = None
    v.redeemed_by = None
    v.booking_id = None
    v.booking_data = {}
    v.payment_id = None
    v.payment_data = None
    v.payment_url = None
    v.payment_provider = None
    v.payment_through = None
    v.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    v.updated_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return v


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recipient_resolved_from_ushauth():
    """
    When recipient_phone is provided, the router calls ushauth get-or-create,
    sets recipient_id from data.id, and stores the full profile in recipient_data.
    """
    captured: dict[str, Any] = {}

    async def mock_create_voucher(**kwargs):
        captured.update(kwargs)
        return _make_mock_voucher()

    async def mock_get_or_create(phone_number, full_name="", settings=None):
        return USHAUTH_PROFILE

    mock_svc = MagicMock()
    mock_svc.create_voucher = mock_create_voucher
    session = AsyncMock()

    with (
        patch("app.voucher.api.router.ushauth_client.get_or_create_customer", new=mock_get_or_create),
        patch("app.voucher.api.router.GiftVoucherService", return_value=mock_svc),
        patch("app.voucher.api.router.get_settings", return_value=MagicMock(
            USHSPA_TOKEN="test-token", GATEWAY_TIMEOUT=5,
            ushauth_base_url="http://ushauth.local",
        )),
    ):
        await create_gift_voucher(
            body=VALID_REQUEST,
            current_user=MOCK_USER,
            session=session,
        )

    assert captured.get("recipient_id") == uuid.UUID(RECIPIENT_ID)
    rd = captured.get("recipient_data", {})
    assert rd.get("id") == RECIPIENT_ID
    assert rd.get("name") == "Alice Smith"
    assert rd.get("phone_number") == "+96541028983"
    assert rd.get("email") == "alice@example.com"


@pytest.mark.asyncio
async def test_ushauth_failure_is_non_fatal():
    """
    If the ushauth call raises, voucher creation still succeeds using
    whatever the caller supplied in recipient_id / recipient_data.
    """
    captured: dict[str, Any] = {}

    async def mock_create_voucher(**kwargs):
        captured.update(kwargs)
        return _make_mock_voucher()

    async def mock_get_or_create_raises(phone_number, full_name="", settings=None):
        raise ConnectionError("ushauth unreachable")

    mock_svc = MagicMock()
    mock_svc.create_voucher = mock_create_voucher
    session = AsyncMock()

    with (
        patch("app.voucher.api.router.ushauth_client.get_or_create_customer", new=mock_get_or_create_raises),
        patch("app.voucher.api.router.GiftVoucherService", return_value=mock_svc),
        patch("app.voucher.api.router.get_settings", return_value=MagicMock(
            USHSPA_TOKEN="test-token", GATEWAY_TIMEOUT=5,
            ushauth_base_url="http://ushauth.local",
        )),
    ):
        resp = await create_gift_voucher(
            body=VALID_REQUEST,
            current_user=MOCK_USER,
            session=session,
        )

    # Voucher still created; recipient_id falls back to body value (None)
    assert captured.get("recipient_id") is None
    # recipient_data falls back to body-supplied value
    assert captured.get("recipient_data", {}).get("name") == "Alice Smith"


@pytest.mark.asyncio
async def test_no_ushauth_call_when_no_recipient_phone():
    """When recipient_phone is absent, ushauth is never called."""
    call_count = 0

    async def mock_get_or_create(phone_number, full_name="", settings=None):
        nonlocal call_count
        call_count += 1
        return USHAUTH_PROFILE

    async def mock_create_voucher(**kwargs):
        return _make_mock_voucher()

    mock_svc = MagicMock()
    mock_svc.create_voucher = mock_create_voucher
    session = AsyncMock()

    request_no_phone = CreateGiftVoucherRequest(
        service_id=uuid.UUID(SERVICE_ID),
        service_data={"name": "Hot Stone Massage"},
        total_amount=Decimal("45.000"),
        currency="KWD",
        total_duration=60,
        # No recipient_phone
        recipient_data={"name": ""},
        sender_data={"name": "Bob", "phone_number": "+96541028000"},
    )

    with (
        patch("app.voucher.api.router.ushauth_client.get_or_create_customer", new=mock_get_or_create),
        patch("app.voucher.api.router.GiftVoucherService", return_value=mock_svc),
        patch("app.voucher.api.router.get_settings", return_value=MagicMock(
            USHSPA_TOKEN="test-token", GATEWAY_TIMEOUT=5,
            ushauth_base_url="http://ushauth.local",
        )),
    ):
        await create_gift_voucher(
            body=request_no_phone,
            current_user=MOCK_USER,
            session=session,
        )

    assert call_count == 0, "ushauth must NOT be called when recipient_phone is absent"
