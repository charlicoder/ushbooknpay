"""
app/loyalty/api/router.py
──────────────────────────
FastAPI router for the loyalty programme.

Endpoints:
  Customer (requires JWT):
    GET  /api/v1/loyalty/my-account/          — view own balance
    GET  /api/v1/loyalty/my-transactions/     — view own transaction history

  Internal (requires app token — called by ushnotice):
    POST /api/v1/loyalty/internal/credit/     — credit points after booking confirmed
    POST /api/v1/loyalty/internal/cancel/     — reverse points after booking cancelled

  Admin (requires app token):
    GET  /api/v1/loyalty/admin/accounts/      — list all accounts (paginated)
    GET  /api/v1/loyalty/admin/accounts/{customer_id}/ — view any customer's account
    POST /api/v1/loyalty/admin/adjust/        — manual point adjustment
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DBSession, RequireAppToken
from app.core.logging import get_logger
from app.loyalty.application.services import LoyaltyService
from app.loyalty.interfaces.schemas import (
    AdjustPointsRequest,
    CancelPointsRequest,
    CreditPointsRequest,
    CreditPointsResponse,
    RedeemPointsRequest,
    LoyaltyAccountResponse,
    LoyaltyTransactionListResponse,
    LoyaltyTransactionResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/loyalty", tags=["Loyalty"])


# ── Dependency ─────────────────────────────────────────────────────────────────


async def get_loyalty_service(session: DBSession) -> LoyaltyService:
    return LoyaltyService(session=session)


LoyaltyServiceDep = Annotated[LoyaltyService, Depends(get_loyalty_service)]


def _account_to_response(account: Any) -> LoyaltyAccountResponse:
    """Map ORM LoyaltyAccount to response schema."""
    now = datetime.now(tz=timezone.utc)
    is_expired = (
        account.points_expire_at is not None
        and account.points_expire_at.astimezone(timezone.utc) <= now
    )
    return LoyaltyAccountResponse(
        id=str(account.id),
        customer_id=str(account.customer_id),
        balance_points=account.balance_points,
        total_earned=account.total_earned,
        total_redeemed=account.total_redeemed,
        points_expire_at=account.points_expire_at,
        is_expired=is_expired,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _txn_to_response(txn: Any) -> LoyaltyTransactionResponse:
    """Map ORM LoyaltyTransaction to response schema."""
    return LoyaltyTransactionResponse(
        id=str(txn.id),
        transaction_type=txn.transaction_type,
        points=txn.points,
        booking_id=str(txn.booking_id) if txn.booking_id else None,
        booking_number=txn.booking_number,
        description=txn.description,
        created_at=txn.created_at,
        created_by=txn.created_by,
    )


# ── Customer endpoints ─────────────────────────────────────────────────────────


@router.get(
    "/my-account/",
    response_model=LoyaltyAccountResponse,
    summary="My loyalty account",
    description="Return the authenticated customer's loyalty balance and expiry.",
)
async def get_my_account(
    current_user: CurrentUser,
    loyalty_service: LoyaltyServiceDep,
) -> LoyaltyAccountResponse:
    customer_id = uuid.UUID(str(current_user.sub))
    account = await loyalty_service.get_account(customer_id)
    if account is None:
        # Return a zero-balance placeholder — no account created until first earn
        return LoyaltyAccountResponse(
            id="",
            customer_id=str(customer_id),
            balance_points=0,
            total_earned=0,
            total_redeemed=0,
            points_expire_at=None,
            is_expired=False,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
    return _account_to_response(account)



@router.post(
    "/redeem/",
    response_model=LoyaltyAccountResponse,
    summary="Redeem loyalty points",
    description="Deduct points from the authenticated customer's loyalty balance.",
)
async def redeem_my_points(
    body: RedeemPointsRequest,
    current_user: CurrentUser,
    loyalty_service: LoyaltyServiceDep,
) -> LoyaltyAccountResponse:
    customer_id = uuid.UUID(str(current_user.sub))
    try:
        account = await loyalty_service.redeem_points(
            customer_id=customer_id,
            cost_in_points=body.cost_in_points,
            booking_id=body.booking_id,
            booking_number=body.booking_number,
            created_by="api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _account_to_response(account)


@router.get(
    "/my-transactions/",
    response_model=LoyaltyTransactionListResponse,
    summary="My loyalty transaction history",
    description="Return the authenticated customer's point history (newest first).",
)
async def get_my_transactions(
    current_user: CurrentUser,
    loyalty_service: LoyaltyServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LoyaltyTransactionListResponse:
    customer_id = uuid.UUID(str(current_user.sub))
    transactions, total = await loyalty_service.get_transactions(
        customer_id, limit=limit, offset=offset
    )
    return LoyaltyTransactionListResponse(
        results=[_txn_to_response(t) for t in transactions],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Internal endpoints (called by ushnotice) ───────────────────────────────────


@router.post(
    "/internal/credit/",
    response_model=CreditPointsResponse,
    status_code=status.HTTP_200_OK,
    summary="[Internal] Credit loyalty points",
    description=(
        "Called by ushnotice after processing a `booking.confirmed` event. "
        "Requires the internal app token."
    ),
)
async def internal_credit_points(
    body: CreditPointsRequest,
    _: RequireAppToken,
    loyalty_service: LoyaltyServiceDep,
) -> CreditPointsResponse:
    try:
        account = await loyalty_service.credit_points(
            customer_id=body.customer_id,
            loyalty_points=body.loyalty_points,
            arrangement_loyalty_points=body.arrangement_loyalty_points,
            booking_id=body.booking_id,
            booking_number=body.booking_number,
            created_by=body.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Resolve effective credited points for the response
    from app.loyalty.domain.rules import resolve_earn_points
    points_credited = resolve_earn_points(body.loyalty_points, body.arrangement_loyalty_points)

    logger.info(
        "loyalty_internal_credit",
        customer_id=str(body.customer_id),
        booking_id=str(body.booking_id) if body.booking_id else None,
        booking_number=body.booking_number,
        points_credited=points_credited,
    )

    return CreditPointsResponse(
        customer_id=str(account.customer_id),
        points_credited=points_credited,
        new_balance=account.balance_points,
        points_expire_at=account.points_expire_at,
        booking_id=str(body.booking_id) if body.booking_id else None,
        booking_number=body.booking_number,
    )


@router.post(
    "/internal/cancel/",
    response_model=CreditPointsResponse,
    status_code=status.HTTP_200_OK,
    summary="[Internal] Reverse points on booking cancellation",
    description=(
        "Called by ushnotice when a `booking.cancelled` event fires for a loyalty-eligible booking. "
        "Requires the internal app token."
    ),
)
async def internal_cancel_points(
    body: CancelPointsRequest,
    _: RequireAppToken,
    loyalty_service: LoyaltyServiceDep,
) -> CreditPointsResponse:
    account = await loyalty_service.cancel_booking_points(
        customer_id=body.customer_id,
        points=body.points,
        booking_id=body.booking_id,
        booking_number=body.booking_number,
        created_by=body.created_by,
    )

    if account is None:
        # No account — nothing to reverse; return zero response
        return CreditPointsResponse(
            customer_id=str(body.customer_id),
            points_credited=0,
            new_balance=0,
            points_expire_at=None,
            booking_id=str(body.booking_id) if body.booking_id else None,
            booking_number=body.booking_number,
        )

    logger.info(
        "loyalty_internal_cancel",
        customer_id=str(body.customer_id),
        booking_id=str(body.booking_id) if body.booking_id else None,
        booking_number=body.booking_number,
        points_reversed=body.points,
    )

    return CreditPointsResponse(
        customer_id=str(account.customer_id),
        points_credited=body.points,  # original points that were reversed
        new_balance=account.balance_points,
        points_expire_at=account.points_expire_at,
        booking_id=str(body.booking_id) if body.booking_id else None,
        booking_number=body.booking_number,
    )


# ── Admin endpoints ────────────────────────────────────────────────────────────


@router.get(
    "/admin/accounts/",
    summary="[Admin] List all loyalty accounts",
    description="Requires the internal app token.",
)
async def admin_list_accounts(
    _: RequireAppToken,
    session: DBSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    from sqlalchemy import func, select

    from app.loyalty.infrastructure.models import LoyaltyAccount

    total_result = await session.execute(select(func.count(LoyaltyAccount.id)))
    total = total_result.scalar_one()

    result = await session.execute(
        select(LoyaltyAccount)
        .order_by(LoyaltyAccount.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    accounts = result.scalars().all()

    return {
        "results": [_account_to_response(a).model_dump() for a in accounts],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/admin/accounts/{customer_id}/",
    response_model=LoyaltyAccountResponse,
    summary="[Admin] View any customer's loyalty account",
    description="Requires the internal app token.",
)
async def admin_get_account(
    customer_id: uuid.UUID,
    _: RequireAppToken,
    loyalty_service: LoyaltyServiceDep,
) -> LoyaltyAccountResponse:
    account = await loyalty_service.get_account(customer_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No loyalty account found for customer {customer_id}.",
        )
    return _account_to_response(account)


@router.get(
    "/admin/accounts/{customer_id}/transactions/",
    response_model=LoyaltyTransactionListResponse,
    summary="[Admin] View any customer's transaction history",
    description="Requires the internal app token.",
)
async def admin_get_transactions(
    customer_id: uuid.UUID,
    _: RequireAppToken,
    loyalty_service: LoyaltyServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LoyaltyTransactionListResponse:
    transactions, total = await loyalty_service.get_transactions(
        customer_id, limit=limit, offset=offset
    )
    return LoyaltyTransactionListResponse(
        results=[_txn_to_response(t) for t in transactions],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/admin/adjust/",
    response_model=LoyaltyAccountResponse,
    summary="[Admin] Manual points adjustment",
    description="Add or remove points from a customer's account. Requires app token.",
)
async def admin_adjust_points(
    body: AdjustPointsRequest,
    _: RequireAppToken,
    loyalty_service: LoyaltyServiceDep,
) -> LoyaltyAccountResponse:
    try:
        account = await loyalty_service.adjust_points(
            customer_id=body.customer_id,
            delta=body.delta,
            description=body.description,
            created_by="admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _account_to_response(account)
