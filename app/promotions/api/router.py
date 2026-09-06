"""
app/promotions/api/router.py
─────────────────────────────
Promotions API — Loyalty Rewards & Tracker endpoints.

Routes (all under /api/v1/promotions/):

  Customer-facing:
    GET  /loyalty/status/                           — all trackers + available rewards
    GET  /loyalty/status/{service_id}/              — tracker for a specific service
    GET  /loyalty/rewards/                          — list my rewards (filterable by status)
    POST /loyalty/rewards/{reward_id}/redeem/       — redeem a reward

  Admin (requires USHSPA-TOKEN):
    GET  /admin/loyalty/rewards/                    — all customers' rewards (paginated)
    GET  /admin/loyalty/trackers/                   — all customers' trackers (paginated, filterable)
    POST /admin/loyalty/trackers/                   — create a tracker manually
    PATCH /admin/loyalty/trackers/{tracker_id}/     — update booking_count / bookings_required
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DBSession, RequireAppToken
from app.promotions.application.loyalty_service import LoyaltyService
from app.promotions.domain.value_objects import LoyaltyRewardStatus
from app.promotions.interfaces.schemas import (
    CreateLoyaltyTrackerRequest,
    LoyaltyRewardResponse,
    LoyaltyStatusResponse,
    LoyaltyTrackerResponse,
    PaginatedRewardsResponse,
    PaginatedTrackersResponse,
    RecordLoyaltyBookingRequest,
    RecordLoyaltyBookingResponse,
    RedeemRewardRequest,
    UpdateLoyaltyTrackerRequest,
)

router = APIRouter(prefix="/promotions", tags=["Promotions – Loyalty"])


# ── Helper ─────────────────────────────────────────────────────────────────────

def _tracker_to_schema(t: object) -> LoyaltyTrackerResponse:
    return LoyaltyTrackerResponse(
        id=t.id,
        customer_id=t.customer_id,
        service_id=t.service_id,
        service_arrangement_id=t.service_arrangement_id,
        service_name=t.service_name,
        booking_count=t.booking_count,
        bookings_required=t.bookings_required,
        bookings_remaining=t.bookings_remaining,
        progress_percentage=t.progress_percentage,
        total_bookings=t.total_bookings,
        total_rewards_earned=t.total_rewards_earned,
        updated_at=t.updated_at,
    )


def _reward_to_schema(r: object) -> LoyaltyRewardResponse:
    return LoyaltyRewardResponse(
        id=r.id,
        customer_id=r.customer_id,
        service_id=r.service_id,
        service_arrangement_id=r.service_arrangement_id,
        service_name=r.service_name,
        status=r.status,
        earned_from_booking_id=r.earned_from_booking_id,
        redeemed_in_booking_id=r.redeemed_in_booking_id,
        redeemed_at=r.redeemed_at,
        expires_at=r.expires_at,
        created_at=r.created_at,
    )


# ── Customer endpoints ─────────────────────────────────────────────────────────

@router.get(
    "/loyalty/status/",
    summary="My loyalty status",
    description=(
        "Returns all loyalty trackers and available rewards for the authenticated customer. "
        "Only branch bookings count towards loyalty."
    ),
    response_model=LoyaltyStatusResponse,
)
async def get_my_loyalty_status(
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Return all loyalty trackers and available rewards for the current user."""
    customer_id = uuid.UUID(current_user.sub)
    svc = LoyaltyService(session)

    trackers = await svc.list_trackers(customer_id)
    available_rewards = await svc.list_customer_rewards(
        customer_id, status=LoyaltyRewardStatus.AVAILABLE.value
    )

    return JSONResponse(
        content=LoyaltyStatusResponse(
            trackers=[_tracker_to_schema(t) for t in trackers],
            available_rewards=[_reward_to_schema(r) for r in available_rewards],
            total_available_rewards=len(available_rewards),
        ).model_dump(mode="json")
    )


@router.get(
    "/loyalty/status/{service_id}/",
    summary="My loyalty status for a specific service",
    response_model=LoyaltyTrackerResponse,
)
async def get_loyalty_status_for_service(
    service_id: uuid.UUID,
    current_user: CurrentUser,
    session: DBSession,
    service_arrangement_id: uuid.UUID | None = Query(
        default=None,
        description="Optional: narrow to a specific service arrangement.",
    ),
) -> JSONResponse:
    """Return the loyalty tracker for a specific (service, arrangement) pair."""
    customer_id = uuid.UUID(current_user.sub)
    svc = LoyaltyService(session)

    tracker = await svc.get_tracker(
        customer_id=customer_id,
        service_id=service_id,
        service_arrangement_id=service_arrangement_id,
    )

    if tracker is None:
        return JSONResponse(
            content={
                "customer_id": str(customer_id),
                "service_id": str(service_id),
                "service_arrangement_id": str(service_arrangement_id) if service_arrangement_id else None,
                "booking_count": 0,
                "bookings_required": 5,
                "bookings_remaining": 5,
                "progress_percentage": 0.0,
                "total_bookings": 0,
                "total_rewards_earned": 0,
                "message": "No loyalty tracker found. Book this service at a branch to start earning rewards.",
            }
        )

    return JSONResponse(content=_tracker_to_schema(tracker).model_dump(mode="json"))


@router.get(
    "/loyalty/rewards/",
    summary="My loyalty rewards",
    description="List loyalty rewards for the authenticated customer.",
    response_model=list[LoyaltyRewardResponse],
)
async def list_my_rewards(
    current_user: CurrentUser,
    session: DBSession,
    status: str | None = Query(
        default=None,
        description="Filter by status: available, redeemed, expired, cancelled",
    ),
) -> JSONResponse:
    """Return rewards for the current user."""
    customer_id = uuid.UUID(current_user.sub)
    svc = LoyaltyService(session)

    # Validate status filter
    valid_statuses = {s.value for s in LoyaltyRewardStatus}
    if status and status not in valid_statuses:
        raise HTTPException(
            status_code=status_code_422,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    rewards = await svc.list_customer_rewards(customer_id=customer_id, status=status)

    return JSONResponse(
        content=[_reward_to_schema(r).model_dump(mode="json") for r in rewards]
    )


@router.post(
    "/loyalty/rewards/{reward_id}/redeem/",
    summary="Redeem a loyalty reward",
    description=(
        "Mark a loyalty reward as redeemed. "
        "Optionally link it to a booking ID (the free booking being created)."
    ),
)
async def redeem_loyalty_reward(
    reward_id: uuid.UUID,
    body: RedeemRewardRequest,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Redeem a loyalty reward for the current user."""
    customer_id = uuid.UUID(current_user.sub)
    svc = LoyaltyService(session)

    success, error, reward = await svc.redeem_reward(
        reward_id=reward_id,
        customer_id=customer_id,
        redeemed_in_booking_id=body.booking_id,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    return JSONResponse(
        content={
            "success": True,
            "reward": _reward_to_schema(reward).model_dump(mode="json"),
        }
    )


# ── Admin endpoints ────────────────────────────────────────────────────────────

@router.get(
    "/admin/loyalty/rewards/",
    summary="[Admin] List all loyalty rewards",
    description=(
        "Admin endpoint — lists all customers' loyalty rewards. "
        "Requires USHSPA-TOKEN. Supports status filter and pagination."
    ),
    response_model=PaginatedRewardsResponse,
)
async def admin_list_all_rewards(
    _: RequireAppToken,
    session: DBSession,
    status: str | None = Query(default=None, description="Filter by status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """Admin: return all loyalty rewards across all customers."""
    svc = LoyaltyService(session)

    valid_statuses = {s.value for s in LoyaltyRewardStatus}
    if status and status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    rewards, total = await svc.list_all_rewards_admin(
        status=status, limit=limit, offset=offset
    )

    return JSONResponse(
        content=PaginatedRewardsResponse(
            items=[_reward_to_schema(r) for r in rewards],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


# ── Admin: Tracker endpoints ───────────────────────────────────────────────────

@router.get(
    "/admin/loyalty/trackers/",
    summary="[Admin] List all loyalty trackers",
    description=(
        "Admin endpoint — lists loyalty trackers across all customers. "
        "Filterable by customer_id or service_id. Supports pagination. "
        "Requires USHSPA-TOKEN."
    ),
    response_model=PaginatedTrackersResponse,
)
async def admin_list_all_trackers(
    _: RequireAppToken,
    session: DBSession,
    customer_id: uuid.UUID | None = Query(default=None, description="Filter by customer UUID"),
    service_id: uuid.UUID | None = Query(default=None, description="Filter by service UUID"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """Admin: return all loyalty trackers with optional filters."""
    svc = LoyaltyService(session)
    trackers, total = await svc.list_all_trackers_admin(
        customer_id=customer_id,
        service_id=service_id,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(
        content=PaginatedTrackersResponse(
            items=[_tracker_to_schema(t) for t in trackers],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.post(
    "/admin/loyalty/trackers/",
    summary="[Admin] Create a loyalty tracker",
    description=(
        "Admin endpoint — manually create a loyalty tracker for a customer/service pair. "
        "Useful for seeding loyalty data or correcting missing trackers. "
        "Returns 409 if a tracker already exists for the same combination. "
        "Requires USHSPA-TOKEN."
    ),
    response_model=LoyaltyTrackerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_tracker(
    body: CreateLoyaltyTrackerRequest,
    _: RequireAppToken,
    session: DBSession,
) -> JSONResponse:
    """Admin: create a loyalty tracker directly."""
    svc = LoyaltyService(session)
    try:
        tracker = await svc.create_tracker_admin(
            customer_id=body.customer_id,
            service_id=body.service_id,
            service_arrangement_id=body.service_arrangement_id,
            service_name=body.service_name,
            bookings_required=body.bookings_required,
            booking_count=body.booking_count,
            total_bookings=body.total_bookings,
        )
        tracker_data = _tracker_to_schema(tracker)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=tracker_data.model_dump(mode="json"),
    )


@router.patch(
    "/admin/loyalty/trackers/{tracker_id}/",
    summary="[Admin] Update a loyalty tracker",
    description=(
        "Admin endpoint — update booking_count and/or bookings_required on a tracker. "
        "Only provided fields are updated (partial update). "
        "Requires USHSPA-TOKEN."
    ),
    response_model=LoyaltyTrackerResponse,
)
async def admin_update_tracker(
    tracker_id: uuid.UUID,
    body: UpdateLoyaltyTrackerRequest,
    _: RequireAppToken,
    session: DBSession,
) -> JSONResponse:
    """Admin: patch a loyalty tracker's counters or threshold."""
    svc = LoyaltyService(session)
    try:
        tracker = await svc.update_tracker_admin(
            tracker_id=tracker_id,
            booking_count=body.booking_count,
            bookings_required=body.bookings_required,
        )
        tracker_data = _tracker_to_schema(tracker)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    return JSONResponse(
        content=tracker_data.model_dump(mode="json"),
    )


# ── Internal endpoints ─────────────────────────────────────────────────────────

@router.post(
    "/internal/loyalty/record/",
    summary="[Internal] Record a confirmed booking for loyalty tracking",
    description=(
        "Internal endpoint called when a booking is confirmed. "
        "Increments loyalty tracker for branch bookings, creates a LoyaltyReward on 5th booking, "
        "resets tracker booking_count to 0, and emits loyalty.rewarded SQS event."
    ),
    response_model=RecordLoyaltyBookingResponse,
)
async def internal_record_loyalty_booking(
    body: RecordLoyaltyBookingRequest,
    session: DBSession,
) -> JSONResponse:
    """Record a confirmed booking for loyalty tracking and emit reward SQS event if threshold reached."""
    # ── is_eligible_for_loyalty guard ────────────────────────────────────────
    # Only services explicitly marked as eligible may accumulate loyalty points.
    # Callers (ushnotice) pass this flag from the booking event — if it's False,
    # skip silently and return an empty response without touching the DB.
    if not body.is_eligible_for_loyalty:
        return JSONResponse(
            content=RecordLoyaltyBookingResponse(
                tracker=None,  # type: ignore[arg-type]
                reward=None,
                reward_issued=False,
            ).model_dump(mode="json")
        )

    svc = LoyaltyService(session)
    try:
        tracker, reward = await svc.record_confirmed_booking(
            customer_id=body.customer_id,
            service_id=body.service_id,
            service_arrangement_id=body.service_arrangement_id,
            booking_id=body.booking_id,
            booking_type=body.booking_type,
            customer_name=body.customer_name,
            customer_email=body.customer_email,
            customer_phone=body.customer_phone,
            service_name=body.service_name,
        )
        tracker_data = _tracker_to_schema(tracker)
        reward_data = _reward_to_schema(reward) if reward else None
        await session.commit()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return JSONResponse(
        content=RecordLoyaltyBookingResponse(
            tracker=tracker_data,
            reward=reward_data,
            reward_issued=reward_data is not None,
        ).model_dump(mode="json")
    )


# Fix for variable name collision with 'status' query param vs fastapi.status
status_code_422 = status.HTTP_422_UNPROCESSABLE_ENTITY
