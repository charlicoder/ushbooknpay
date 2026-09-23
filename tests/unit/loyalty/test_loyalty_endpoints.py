"""
tests/unit/loyalty/test_loyalty_endpoints.py
─────────────────────────────────────────────
Unit tests for loyalty endpoints, TokenPayload authentication, and domain rules.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.security import TokenPayload
from app.loyalty.api.router import (
    get_my_account,
    get_my_transactions,
    internal_cancel_points,
    internal_credit_points,
)
from app.loyalty.application.services import LoyaltyService
from app.loyalty.domain.rules import (
    assert_sufficient_balance,
    compute_expiry,
    is_account_expired,
    resolve_earn_points,
)
from app.loyalty.infrastructure.models import LoyaltyAccount, LoyaltyTransaction
from app.loyalty.interfaces.schemas import (
    CancelPointsRequest,
    CreditPointsRequest,
)


# ── Domain Rules Tests ─────────────────────────────────────────────────────────

def test_resolve_earn_points():
    # Arrangement override takes precedence if > 0
    assert resolve_earn_points(loyalty_points=50, arrangement_loyalty_points=100) == 100
    # None or 0 falls back to service loyalty_points
    assert resolve_earn_points(loyalty_points=50, arrangement_loyalty_points=None) == 50
    assert resolve_earn_points(loyalty_points=50, arrangement_loyalty_points=0) == 50
    assert resolve_earn_points(loyalty_points=0, arrangement_loyalty_points=None) == 0


def test_compute_expiry():
    exp = compute_expiry(expiry_days=180)
    assert exp.tzinfo is not None
    assert (exp - datetime.now(timezone.utc)).days in (179, 180)


def test_assert_sufficient_balance():
    assert_sufficient_balance(balance_points=100, cost_in_points=50)
    with pytest.raises(ValueError, match="Insufficient loyalty points"):
        assert_sufficient_balance(balance_points=30, cost_in_points=50)



# ── TokenPayload & Router Endpoint Tests ──────────────────────────────────────

def test_token_payload_user_id_property():
    cid = str(uuid.uuid4())
    payload = TokenPayload(sub=cid)
    assert payload.sub == cid
    assert payload.user_id == cid


@pytest.mark.asyncio
async def test_get_my_account_not_found_returns_zero_placeholder():
    cid = uuid.uuid4()
    current_user = TokenPayload(sub=str(cid))

    mock_service = MagicMock(spec=LoyaltyService)
    mock_service.get_account = AsyncMock(return_value=None)

    res = await get_my_account(current_user=current_user, loyalty_service=mock_service)
    assert res.customer_id == str(cid)
    assert res.balance_points == 0
    assert res.total_earned == 0
    assert res.is_expired is False
    mock_service.get_account.assert_awaited_once_with(cid)


@pytest.mark.asyncio
async def test_get_my_account_existing_account():
    cid = uuid.uuid4()
    current_user = TokenPayload(sub=str(cid))

    account = LoyaltyAccount(
        id=uuid.uuid4(),
        customer_id=cid,
        balance_points=250,
        total_earned=500,
        total_redeemed=250,
        points_expire_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_service = MagicMock(spec=LoyaltyService)
    mock_service.get_account = AsyncMock(return_value=account)

    res = await get_my_account(current_user=current_user, loyalty_service=mock_service)
    assert res.customer_id == str(cid)
    assert res.balance_points == 250
    assert res.total_earned == 500
    assert res.total_redeemed == 250


@pytest.mark.asyncio
async def test_get_my_transactions():
    cid = uuid.uuid4()
    current_user = TokenPayload(sub=str(cid))

    tx = LoyaltyTransaction(
        id=uuid.uuid4(),
        customer_id=cid,
        transaction_type="earn",
        points=100,
        booking_id=uuid.uuid4(),
        booking_number="BK-1001",
        description="Booking confirmed",
        created_at=datetime.now(timezone.utc),
        created_by="ushnotice",
    )

    mock_service = MagicMock(spec=LoyaltyService)
    mock_service.get_transactions = AsyncMock(return_value=([tx], 1))

    res = await get_my_transactions(
        current_user=current_user,
        loyalty_service=mock_service,
        limit=20,
        offset=0,
    )
    assert res.total == 1
    assert len(res.results) == 1
    assert res.results[0].booking_number == "BK-1001"
    assert res.results[0].points == 100
    mock_service.get_transactions.assert_awaited_once_with(cid, limit=20, offset=0)


@pytest.mark.asyncio
async def test_internal_credit_points():
    cid = uuid.uuid4()
    bid = uuid.uuid4()

    account = LoyaltyAccount(
        id=uuid.uuid4(),
        customer_id=cid,
        balance_points=100,
        total_earned=100,
        total_redeemed=0,
        points_expire_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_service = MagicMock(spec=LoyaltyService)
    mock_service.credit_points = AsyncMock(return_value=account)

    body = CreditPointsRequest(
        customer_id=cid,
        booking_id=bid,
        booking_number="BK-1002",
        loyalty_points=100,
        arrangement_loyalty_points=None,
    )

    res = await internal_credit_points(body=body, _=None, loyalty_service=mock_service)
    assert res.customer_id == str(cid)
    assert res.points_credited == 100
    assert res.new_balance == 100
    assert res.booking_number == "BK-1002"


@pytest.mark.asyncio
async def test_internal_cancel_points():
    cid = uuid.uuid4()
    bid = uuid.uuid4()

    account = LoyaltyAccount(
        id=uuid.uuid4(),
        customer_id=cid,
        balance_points=50,
        total_earned=100,
        total_redeemed=0,
        points_expire_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_service = MagicMock(spec=LoyaltyService)
    mock_service.cancel_booking_points = AsyncMock(return_value=account)

    body = CancelPointsRequest(
        customer_id=cid,
        points=50,
        booking_id=bid,
        booking_number="BK-1003",
    )

    res = await internal_cancel_points(body=body, _=None, loyalty_service=mock_service)
    assert res.customer_id == str(cid)
    assert res.points_credited == 50
    assert res.new_balance == 50
    assert res.booking_number == "BK-1003"


from app.loyalty.api.router import redeem_my_points
from app.loyalty.interfaces.schemas import RedeemPointsRequest

@pytest.mark.asyncio
async def test_redeem_my_points_success():
    cid = uuid.uuid4()
    bid = uuid.uuid4()
    current_user = TokenPayload(sub=str(cid))

    account = LoyaltyAccount(
        id=uuid.uuid4(),
        customer_id=cid,
        balance_points=120,
        total_earned=220,
        total_redeemed=100,
        points_expire_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    mock_service = MagicMock(spec=LoyaltyService)
    mock_service.redeem_points = AsyncMock(return_value=account)

    body = RedeemPointsRequest(
        cost_in_points=100,
        booking_id=bid,
        booking_number="BK-2001",
    )

    res = await redeem_my_points(body=body, current_user=current_user, loyalty_service=mock_service)
    assert res.customer_id == str(cid)
    assert res.balance_points == 120
    assert res.total_redeemed == 100
    mock_service.redeem_points.assert_awaited_once_with(
        customer_id=cid,
        cost_in_points=100,
        booking_id=bid,
        booking_number="BK-2001",
        created_by="api",
    )
