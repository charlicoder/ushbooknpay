"""
app/course/api/router.py
────────────────────────
Course subscription API (contract D), mounted at ``/api/v1/course-subscriptions``.

Every route requires an employee Bearer token; codenames are checked with
``TokenPayload.has_permission`` (superusers / ``*`` always pass). The customer
app uses the read-only ``GET /api/v1/my-course-subscriptions/`` (own rows only).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DBSession, USHAuthDep
from app.common.pagination import make_paginated_response
from app.common.utils import utcnow
from app.core.security import TokenPayload, require_permission
from app.course.api.schemas import (
    CancelRequest,
    ConfirmPaymentRequest,
    CreateCourseSubscriptionRequest,
    PaymentLinkRequest,
    RefundRequest,
    serialize_subscription,
    serialize_usage,
)
from app.course.application.services import CourseSubscriptionService
from app.course.infrastructure.repository import CourseSubscriptionRepository

router = APIRouter(prefix="/course-subscriptions", tags=["Course Subscriptions"])

PERM_VIEW = "course_subscriptions.view"
PERM_SELL = "course_subscriptions.sell"
PERM_REDEEM = "course_subscriptions.redeem"
PERM_REFUND = "course_subscriptions.refund"


def _actor(user: TokenPayload) -> tuple[str, dict[str, Any]]:
    return str(user.sub), {
        "id": str(user.sub),
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "email": user.email or "",
        "phone_number": user.phone_number or "",
        "user_type": user.user_type,
        "role": user.role,
    }


def _ok(data: Any, code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(status_code=code, content={"success": True, "data": data})


async def _detail(service: CourseSubscriptionService, session: Any, sub: Any) -> dict[str, Any]:
    usages = await service.list_usages(sub)
    live = await CourseSubscriptionRepository(session).booking_statuses([u.booking_id for u in usages])
    return serialize_subscription(sub, [serialize_usage(u, live.get(str(u.booking_id))) for u in usages])


async def _list(
    request: Request,
    session: Any,
    *,
    page: int,
    page_size: int,
    **filters: Any,
) -> JSONResponse:
    rows, total = await CourseSubscriptionRepository(session).list_subscriptions(
        page=page, page_size=page_size, now=utcnow(), **filters
    )
    paginated = make_paginated_response(
        [serialize_subscription(s) for s in rows],
        count=total,
        page=page,
        page_size=page_size,
        base_url=str(request.url.remove_query_params(["page", "page_size"])),
    )
    return JSONResponse(content=paginated.model_dump(mode="json"))


@router.get("/", summary="List course subscriptions")
async def list_course_subscriptions(
    request: Request,
    session: DBSession,
    _user: TokenPayload = Depends(require_permission(PERM_VIEW)),
    course_id: uuid.UUID | None = Query(default=None),
    customer_id: uuid.UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    expired: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    return await _list(
        request,
        session,
        page=page,
        page_size=page_size,
        course_id=course_id,
        customer_id=customer_id,
        status=status_filter,
        expired=expired,
        search=search,
    )


@router.post("/", summary="Sell a course (create a subscription)", status_code=status.HTTP_201_CREATED)
async def create_course_subscription(
    body: CreateCourseSubscriptionRequest,
    request: Request,
    session: DBSession,
    ushauth: USHAuthDep,
    user: TokenPayload = Depends(require_permission(PERM_SELL)),
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    service = CourseSubscriptionService(session, ushauth_client=ushauth)
    sub, created = await service.create_subscription(
        customer_id=body.customer_id,
        course_id=body.course_id,
        payment_option=body.payment_option,
        payment_gateway=body.payment_gateway,
        discount=body.discount,
        idempotency_key=body.idempotency_key or request.headers.get("Idempotency-Key"),
        notes=body.notes,
        actor_id=actor_id,
        actor_data=actor_data,
    )
    return _ok(serialize_subscription(sub), status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@router.get("/customers/{customer_id}/", summary="Course subscriptions of a customer")
async def list_customer_course_subscriptions(
    customer_id: uuid.UUID,
    request: Request,
    session: DBSession,
    _user: TokenPayload = Depends(require_permission(PERM_VIEW)),
    redeemable: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
) -> JSONResponse:
    return await _list(
        request, session, page=page, page_size=page_size, customer_id=customer_id, redeemable=redeemable
    )


@router.get("/{subscription_id}/", summary="Course subscription detail (with sessions)")
async def get_course_subscription(
    subscription_id: uuid.UUID,
    session: DBSession,
    _user: TokenPayload = Depends(require_permission(PERM_VIEW)),
) -> JSONResponse:
    service = CourseSubscriptionService(session)
    sub = await service.get(subscription_id)
    return _ok(await _detail(service, session, sub))


@router.get("/{subscription_id}/sessions/", summary="Session history of a course subscription")
async def list_course_subscription_sessions(
    subscription_id: uuid.UUID,
    session: DBSession,
    _user: TokenPayload = Depends(require_permission(PERM_VIEW)),
) -> JSONResponse:
    service = CourseSubscriptionService(session)
    sub = await service.get(subscription_id)
    return _ok((await _detail(service, session, sub))["sessions"])


@router.post("/{subscription_id}/confirm-payment/", summary="Confirm a desk payment (activates)")
async def confirm_course_payment(
    subscription_id: uuid.UUID,
    body: ConfirmPaymentRequest,
    session: DBSession,
    user: TokenPayload = Depends(require_permission(PERM_SELL)),
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    service = CourseSubscriptionService(session)
    sub, _payment, _activated = await service.activate(
        subscription_id,
        payment_provider=body.payment_provider,
        payment_method=body.payment_method,
        payment_gateway=body.payment_gateway,
        transaction_reference=body.transaction_reference,
        payment_through=body.payment_through or "ushdesk",
        actor_id=actor_id,
        actor_data=actor_data,
    )
    return _ok(await _detail(service, session, sub))


@router.post("/{subscription_id}/payment-link/", summary="Create or renew the payment link")
async def create_course_payment_link(
    subscription_id: uuid.UUID,
    body: PaymentLinkRequest,
    session: DBSession,
    user: TokenPayload = Depends(require_permission(PERM_SELL)),
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    service = CourseSubscriptionService(session)
    sub = await service.create_payment_link(
        subscription_id, payment_gateway=body.payment_gateway, actor_id=actor_id, actor_data=actor_data
    )
    return _ok(serialize_subscription(sub))


@router.post("/{subscription_id}/cancel/", summary="Cancel an unpaid course subscription")
async def cancel_course_subscription(
    subscription_id: uuid.UUID,
    body: CancelRequest,
    session: DBSession,
    _user: TokenPayload = Depends(require_permission(PERM_SELL)),
) -> JSONResponse:
    sub = await CourseSubscriptionService(session).cancel(subscription_id, reason=body.reason)
    return _ok(serialize_subscription(sub))


@router.post("/{subscription_id}/refund/", summary="Refund a course subscription (full)")
async def refund_course_subscription(
    subscription_id: uuid.UUID,
    body: RefundRequest,
    session: DBSession,
    user: TokenPayload = Depends(require_permission(PERM_REFUND)),
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    sub = await CourseSubscriptionService(session).refund(
        subscription_id,
        reason=body.reason,
        refund_method=body.refund_method,
        actor_id=actor_id,
        actor_data=actor_data,
    )
    return _ok(serialize_subscription(sub))


async def list_my_course_subscriptions(
    request: Request,
    current_user: CurrentUser,
    session: DBSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """Customer app: read-only list of the caller's own subscriptions."""
    try:
        customer_id = uuid.UUID(str(current_user.sub))
    except ValueError:
        return _ok([])
    return await _list(request, session, page=page, page_size=page_size, customer_id=customer_id)

