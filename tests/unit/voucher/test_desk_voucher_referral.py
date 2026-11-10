"""Unit tests verifying that vouchers created via desk/ushdesk automatically
attribute 'referenced_by' and 'referenced_by_name' to 'created_by' / 'created_by_name'
when no explicit referral code is provided.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.voucher.application.desk_list import DeskFilters, DeskVoucherItem, _row_to_item, _slim_columns
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import VoucherPaymentThrough
from app.voucher.infrastructure.models import GiftVoucher


@pytest.mark.asyncio
async def test_voucher_service_desk_auto_sets_referenced_by_from_created_by():
    """GiftVoucherService.create_voucher should set referenced_by from created_by for desk vouchers."""
    session = AsyncMock()
    svc = GiftVoucherService(session)

    creator_id = uuid.uuid4()
    creator_name = "Reception Staff"

    with (
        patch.object(svc._repo, "add", side_effect=lambda v: v),
        patch.object(svc._repo, "generate_voucher_number", new=AsyncMock(return_value="GV-202611-0001")),
        patch.object(svc._repo, "flush_in_savepoint", new=AsyncMock()),
    ):
        voucher = await svc.create_voucher(
            total_amount=Decimal("25.000"),
            sender_id=uuid.uuid4(),
            sender_data={"name": "Customer Name", "phone_number": "+96512345678"},
            created_by=creator_id,
            created_by_name=creator_name,
            payment_through="desk",  # will normalise to ushdesk
        )

        assert voucher.referenced_by == creator_id
        assert voucher.referenced_by_name == creator_name


@pytest.mark.asyncio
async def test_voucher_service_preserves_explicit_referral_when_present():
    """If an explicit referral is provided, it should not be overwritten by created_by."""
    session = AsyncMock()
    svc = GiftVoucherService(session)

    creator_id = uuid.uuid4()
    affiliate_id = uuid.uuid4()

    with (
        patch.object(svc._repo, "add", side_effect=lambda v: v),
        patch.object(svc._repo, "generate_voucher_number", new=AsyncMock(return_value="GV-202611-0002")),
        patch.object(svc._repo, "flush_in_savepoint", new=AsyncMock()),
    ):
        voucher = await svc.create_voucher(
            total_amount=Decimal("25.000"),
            sender_id=uuid.uuid4(),
            sender_data={"name": "Customer Name", "phone_number": "+96512345678"},
            created_by=creator_id,
            created_by_name="Desk Staff",
            payment_through="ushdesk",
            referenced_by=affiliate_id,
            referenced_by_name="Affiliate Partner",
        )

        assert voucher.referenced_by == affiliate_id
        assert voucher.referenced_by_name == "Affiliate Partner"


def test_desk_list_includes_referral_in_list_view():
    """_slim_columns and _row_to_item should include referenced_by in view='list'."""
    cols = _slim_columns("list")
    col_names = [getattr(c, "key", str(c)) for c in cols]
    assert any("referenced_by" in str(c) for c in col_names)
    assert any("referenced_by_name" in str(c) for c in col_names)

    mock_row = MagicMock()
    ref_id = uuid.uuid4()
    now = datetime(2026, 11, 10, 8, 0, 0, tzinfo=timezone.utc)
    mock_row._mapping = {
        "id": uuid.uuid4(),
        "voucher_number": "GV-123",
        "secret_code": "SEC123",
        "public_token": "TOK123",
        "status": "active",
        "total_amount": "25.000",
        "discount": "0.000",
        "currency": "KWD",
        "total_duration": 60,
        "extra_time": 0,
        "gift_category": "SERVICE",
        "delivery_status": None,
        "delivery_mode": "instant",
        "expire_date": None,
        "created_at": now,
        "updated_at": None,
        "redeemed_at": None,
        "scheduled_delivery_at": None,
        "payment_through": "ushdesk",
        "payment_url": None,
        "payment_provider": "MyFatoorah",
        "created_by": ref_id,
        "created_by_name": "Test Receptionist",
        "created_by_role": "Staff",
        "cbu_name": "Test Receptionist",
        "cbu_role": "Staff",
        "referenced_by": ref_id,
        "referenced_by_name": "Test Receptionist",
        "referral_number": None,
        "activation_datetime": now,
        "recipient_phone": "+96598765432",
        "gift_message": None,
        "gift_from": None,
        "sd_name": "Massage",
        "sd_duration": 60,
        "sd_type": "Spa",
        "snd_name": "Sender",
        "snd_phone": "+96512345678",
        "rcp_name": "Recipient",
        "rcp_phone": "+96598765432",
        "br_name": "Salmiya",
        "svc_names": ["Massage"],
        "addon_durations": [],
    }

    item = _row_to_item(mock_row, "list")
    assert isinstance(item, DeskVoucherItem)
    assert item.referenced_by == ref_id
    assert item.referenced_by_name == "Test Receptionist"
    assert item.activation_datetime == now
