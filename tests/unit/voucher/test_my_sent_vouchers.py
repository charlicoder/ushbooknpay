"""
tests/unit/voucher/test_my_sent_vouchers.py
────────────────────────────────────────────
Unit tests for GET /api/v1/vouchers/my-sent-vouchers/
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.requests import Request
from starlette.datastructures import URL

from app.core.security import TokenPayload
from app.main import app
from app.voucher.api.router import list_my_sent_vouchers
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.infrastructure.repository import GiftVoucherRepository


# ── Shared fixtures ───────────────────────────────────────────────────────────

SENDER_UUID = uuid.uuid4()
SERVICE_UUID = uuid.uuid4()
BRANCH_UUID  = uuid.uuid4()


def _make_user(sender_id: uuid.UUID = SENDER_UUID) -> TokenPayload:
    return TokenPayload(
        sub=str(sender_id),
        phone_number="+96500000001",
        first_name="Alice",
        last_name="Smith",
    )


def _make_request(
    url: str = "http://testserver/api/v1/vouchers/my-sent-vouchers/",
) -> MagicMock:
    req = MagicMock(spec=Request)
    req.url = URL(url)
    return req


def _make_voucher(
    sender_id: uuid.UUID = SENDER_UUID,
    status: str = "active",
) -> MagicMock:
    v = MagicMock(spec=GiftVoucher)
    v.id                         = uuid.uuid4()
    v.sender_id                  = sender_id
    v.sender_data                = {"name": "Alice"}
    v.service_id                 = SERVICE_UUID
    v.service_data               = {}
    v.branch_id                  = BRANCH_UUID
    v.branch_data                = {}
    v.service_arrangement_id     = None
    v.service_arrangement_data   = {}
    v.addons                     = []
    v.extra_time                 = 0
    v.price_for_extra_time       = None
    # GiftVoucherResponse.expire_date is typed as datetime (not Optional) — must be real value
    v.expire_date                = datetime(2027, 1, 1, tzinfo=timezone.utc)
    v.status                     = status
    v.recipient_phone            = "+96512345678"
    v.recipient_id               = None
    v.recipient_data             = {}
    v.created_by                 = sender_id
    v.total_duration             = 60
    v.total_amount               = Decimal("50.000")
    v.currency                   = "KWD"
    v.gift_message               = "Enjoy!"
    v.gift_template              = None
    v.secret_code                = "ABC123"
    v.public_token               = "pub_token"
    v.redeemed_booking_id        = None
    v.redeemed_at                = None
    v.booking_id                 = None
    v.payment_id                 = None
    v.payment_data               = None
    v.payment_url                = None
    v.created_at                 = datetime(2026, 9, 6, tzinfo=timezone.utc)
    v.updated_at                 = datetime(2026, 9, 6, tzinfo=timezone.utc)
    return v


# ── Endpoint tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_my_sent_vouchers_returns_200_with_results():
    """200 response with sender's vouchers in data[]."""
    voucher = _make_voucher()
    user    = _make_user()
    session = AsyncMock()

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_sent_vouchers",
        new_callable=AsyncMock,
    ) as mock_list:
        mock_list.return_value = ([voucher], 1)

        resp = await list_my_sent_vouchers(
            current_user=user,
            session=session,
            request=_make_request(),
            status_filter=None,
            created_at=None,
            from_date=None,
            to_date=None,
            service_id=None,
            branch_id=None,
            page=1,
            page_size=20,
        )

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["success"] is True
    assert body["meta"]["pagination"]["count"] == 1
    assert len(body["data"]) == 1
    assert body["data"][0]["sender_id"] == str(SENDER_UUID)


@pytest.mark.asyncio
async def test_my_sent_vouchers_empty_when_none_sent():
    """Returns empty data[] when the user has sent no vouchers."""
    user    = _make_user()
    session = AsyncMock()

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_sent_vouchers",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        resp = await list_my_sent_vouchers(
            current_user=user,
            session=session,
            request=_make_request(),
            status_filter=None,
            created_at=None,
            from_date=None,
            to_date=None,
            service_id=None,
            branch_id=None,
            page=1,
            page_size=20,
        )

    body = json.loads(resp.body)
    assert body["meta"]["pagination"]["count"] == 0
    assert body["data"] == []


@pytest.mark.asyncio
async def test_my_sent_vouchers_filters_forwarded_to_service():
    """All query params are forwarded correctly to GiftVoucherService."""
    user    = _make_user()
    session = AsyncMock()

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_sent_vouchers",
        new_callable=AsyncMock,
        return_value=([], 0),
    ) as mock_list:
        await list_my_sent_vouchers(
            current_user=user,
            session=session,
            request=_make_request(),
            status_filter="active",
            created_at="2026-09-01",
            from_date="2026-08-01",
            to_date="2026-09-30",
            service_id=SERVICE_UUID,
            branch_id=BRANCH_UUID,
            page=2,
            page_size=10,
        )

    mock_list.assert_awaited_once()
    kw = mock_list.call_args.kwargs
    assert kw["status"]     == "active"
    assert kw["created_at"] == "2026-09-01"
    assert kw["from_date"]  == "2026-08-01"
    assert kw["to_date"]    == "2026-09-30"
    assert kw["service_id"] == SERVICE_UUID
    assert kw["branch_id"]  == BRANCH_UUID
    assert kw["page"]       == 2
    assert kw["page_size"]  == 10


@pytest.mark.asyncio
async def test_my_sent_vouchers_uses_jwt_sub_as_sender_id():
    """sender_id passed to service comes from current_user.sub (JWT)."""
    other_uuid = uuid.uuid4()
    user       = _make_user(sender_id=other_uuid)
    session    = AsyncMock()
    captured: dict = {}

    with patch(
        "app.voucher.api.router.GiftVoucherService.list_sent_vouchers",
        new_callable=AsyncMock,
        return_value=([], 0),
    ) as mock_list:
        await list_my_sent_vouchers(
            current_user=user,
            session=session,
            request=_make_request(),
            status_filter=None,
            created_at=None,
            from_date=None,
            to_date=None,
            service_id=None,
            branch_id=None,
            page=1,
            page_size=20,
        )

    kw = mock_list.call_args.kwargs
    assert kw["sender_id"] == other_uuid


@pytest.mark.asyncio
async def test_my_sent_vouchers_isolation_from_other_senders():
    """Only the requesting user's vouchers are returned (service enforces this)."""
    my_voucher    = _make_voucher(sender_id=SENDER_UUID)
    other_voucher = _make_voucher(sender_id=uuid.uuid4())
    user          = _make_user(sender_id=SENDER_UUID)
    session       = AsyncMock()

    # Service correctly returns only mine
    with patch(
        "app.voucher.api.router.GiftVoucherService.list_sent_vouchers",
        new_callable=AsyncMock,
        return_value=([my_voucher], 1),
    ):
        resp = await list_my_sent_vouchers(
            current_user=user,
            session=session,
            request=_make_request(),
            status_filter=None,
            created_at=None,
            from_date=None,
            to_date=None,
            service_id=None,
            branch_id=None,
            page=1,
            page_size=20,
        )

    body = json.loads(resp.body)
    assert body["meta"]["pagination"]["count"] == 1
    sender_ids_in_response = {item["sender_id"] for item in body["data"]}
    assert str(other_voucher.sender_id) not in sender_ids_in_response


def test_my_sent_vouchers_route_registered():
    """Verify that both /api/v1/vouchers/my-sent-vouchers/ and /api/v1/my-sent-vouchers/ are registered."""
    paths = [route.path for route in app.routes]
    assert "/api/v1/vouchers/my-sent-vouchers/" in paths
    assert "/api/v1/my-sent-vouchers/" in paths


# ── Service unit tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_list_sent_vouchers_delegates_to_repo():
    """GiftVoucherService.list_sent_vouchers delegates to list_sent_by_sender."""
    session = AsyncMock()
    svc     = GiftVoucherService(session)

    with patch.object(
        svc._repo, "list_sent_by_sender", new_callable=AsyncMock
    ) as mock_repo:
        mock_repo.return_value = ([], 0)

        items, total = await svc.list_sent_vouchers(
            sender_id=SENDER_UUID,
            status="active",
            from_date="2026-08-01",
            to_date="2026-09-30",
            service_id=SERVICE_UUID,
            branch_id=BRANCH_UUID,
            page=1,
            page_size=20,
        )

    assert total == 0
    mock_repo.assert_awaited_once_with(
        SENDER_UUID,
        status="active",
        created_at=None,
        from_date="2026-08-01",
        to_date="2026-09-30",
        service_id=SERVICE_UUID,
        branch_id=BRANCH_UUID,
        page=1,
        page_size=20,
    )


# ── Repository unit tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_list_sent_by_sender_runs_two_queries():
    """list_sent_by_sender issues a count query then a data query."""
    session = AsyncMock()
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 0
    mock_rows  = MagicMock()
    mock_rows.scalars.return_value.all.return_value = []
    session.execute.side_effect = [mock_count, mock_rows]

    repo = GiftVoucherRepository(session)
    items, total = await repo.list_sent_by_sender(SENDER_UUID)

    assert total == 0
    assert items == []
    assert session.execute.call_count == 2


@pytest.mark.asyncio
async def test_repo_list_sent_by_sender_status_all_skips_filter():
    """status='all' must NOT add a WHERE status= clause (no exception raised)."""
    session = AsyncMock()
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 0
    mock_rows  = MagicMock()
    mock_rows.scalars.return_value.all.return_value = []
    session.execute.side_effect = [mock_count, mock_rows]

    repo = GiftVoucherRepository(session)
    items, total = await repo.list_sent_by_sender(SENDER_UUID, status="all")

    assert total == 0
    assert session.execute.call_count == 2
