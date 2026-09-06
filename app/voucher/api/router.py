"""
app/voucher/api/router.py
──────────────────────────
Gift Voucher API endpoints.

Routes (all under /api/v1/vouchers/):

  Public / Interservice (require USH_TOKEN):
    GET    /                        — List all gift vouchers (not filtered by created_by)

  Customer-facing (require JWT Bearer):
    POST   /                        — Create a gift voucher (status=created)
    GET    /{voucher_id}/           — Get full voucher detail (sender or admin only)
    GET    /my-vouchers/            — List my received vouchers (I am recipient)
    GET    /my-sent-vouchers/       — List my sent vouchers (I am sender)

  Internal (require USHSPA-TOKEN):
    PATCH  /{voucher_id}/status/    — Update voucher status (payment webhook / booking)
    GET    /admin/                  — Admin: list all vouchers (paginated, filterable)

  Public (no auth):
    GET    /public/{public_token}/  — Public gift card page (no secret_code exposed)
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DBSession, RequireAppToken
from app.clients import ushauth as ushauth_client
from app.common.pagination import make_paginated_response
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.interfaces.schemas import (
    CreateGiftVoucherRequest,
    GiftVoucherListItem,
    GiftVoucherPublicResponse,
    GiftVoucherResponse,
    UpdateGiftVoucherStatusRequest,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/vouchers", tags=["Gift Vouchers"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _voucher_to_response(v: GiftVoucher) -> GiftVoucherResponse:
    """Map ORM GiftVoucher → full GiftVoucherResponse."""
    return GiftVoucherResponse(
        id=v.id,
        service_id=v.service_id,
        service_data=v.service_data or {},
        branch_id=v.branch_id,
        branch_data=v.branch_data or {},
        service_arrangement_id=v.service_arrangement_id,
        service_arrangement_data=v.service_arrangement_data or {},
        addons=v.addons or [],
        extra_time=v.extra_time,
        price_for_extra_time=v.price_for_extra_time,
        expire_date=v.expire_date,
        status=v.status,
        sender_id=v.sender_id,
        sender_data=v.sender_data or {},
        recipient_phone=v.recipient_phone,
        recipient_id=v.recipient_id,
        recipient_data=v.recipient_data or {},
        created_by=v.created_by,
        total_duration=v.total_duration,
        total_amount=str(v.total_amount),
        currency=v.currency,
        gift_message=v.gift_message,
        gift_template=v.gift_template,
        secret_code=v.secret_code,
        public_token=v.public_token,
        redeemed_booking_id=v.redeemed_booking_id,
        redeemed_at=v.redeemed_at,
        booking_id=v.booking_id,
        payment_id=v.payment_id,
        payment_data=v.payment_data,
        payment_url=v.payment_url,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def _voucher_to_public(v: GiftVoucher) -> GiftVoucherPublicResponse:
    """Map ORM GiftVoucher → public (no secret_code) response."""
    # Only expose sender's name on the public page, not phone number
    sender_public: dict[str, Any] = {"name": (v.sender_data or {}).get("name", "")}
    return GiftVoucherPublicResponse(
        id=v.id,
        service_id=v.service_id,
        service_data=v.service_data or {},
        branch_id=v.branch_id,
        branch_data=v.branch_data or {},
        service_arrangement_id=v.service_arrangement_id,
        service_arrangement_data=v.service_arrangement_data or {},
        addons=v.addons or [],
        extra_time=v.extra_time,
        expire_date=v.expire_date,
        status=v.status,
        sender_data=sender_public,
        total_duration=v.total_duration,
        total_amount=str(v.total_amount),
        currency=v.currency,
        gift_message=v.gift_message,
        gift_template=v.gift_template,
        public_token=v.public_token,
        created_at=v.created_at,
    )


def _voucher_to_list_item(v: GiftVoucher) -> GiftVoucherListItem:
    """Map ORM GiftVoucher → lightweight list item."""
    return GiftVoucherListItem(
        id=v.id,
        service_id=v.service_id,
        service_data=v.service_data or {},
        branch_id=v.branch_id,
        branch_data=v.branch_data or {},
        status=v.status,
        total_amount=str(v.total_amount),
        currency=v.currency,
        expire_date=v.expire_date,
        extra_time=v.extra_time,
        price_for_extra_time=v.price_for_extra_time,
        sender_data=v.sender_data or {},
        recipient_phone=v.recipient_phone,
        recipient_id=v.recipient_id,
        recipient_data=v.recipient_data or {},
        gift_message=v.gift_message,
        secret_code=v.secret_code,
        public_token=v.public_token,
        redeemed_at=v.redeemed_at,
        payment_id=v.payment_id,
        payment_data=v.payment_data,
        payment_url=v.payment_url,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


# ── Customer endpoints ─────────────────────────────────────────────────────────


@router.post(
    "/",
    summary="Create a gift voucher",
    description=(
        "Create a new gift voucher for a service. Status starts as 'created'. "
        "After the sender completes payment, update the status to 'active' via "
        "PATCH /{voucher_id}/status/ so the secret code is delivered to the recipient."
    ),
    status_code=status.HTTP_201_CREATED,
)
async def create_gift_voucher(
    body: CreateGiftVoucherRequest,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Create a new gift voucher for the authenticated customer."""
    sender_id = uuid.UUID(current_user.sub)
    settings = get_settings()

    # Build sender_data from both body-supplied data and JWT profile
    sender_data: dict[str, Any] = body.sender_data.model_dump()
    if not sender_data.get("name"):
        full_name = " ".join(
            filter(None, [current_user.first_name, current_user.last_name])
        )
        sender_data["name"] = full_name
    if not sender_data.get("phone_number") and current_user.phone_number:
        sender_data["phone_number"] = current_user.phone_number

    # ── Resolve recipient via ushauth get-or-create ──────────────────────
    # When recipient_phone is supplied, call ushauth to look up or create
    # the recipient customer.  The returned id becomes recipient_id and the
    # full profile is stored as recipient_data (merging any name already
    # provided by the caller).
    recipient_id: uuid.UUID | None = body.recipient_id
    recipient_data: dict[str, Any] = body.recipient_data.model_dump()

    if body.recipient_phone:
        try:
            # Use name already in recipient_data as full_name hint
            full_name_hint = recipient_data.get("name", "")
            customer = await ushauth_client.get_or_create_customer(
                phone_number=body.recipient_phone,
                full_name=full_name_hint,
                settings=settings,
            )
            # Override recipient_id with the authoritative ushauth UUID
            if customer.get("id"):
                recipient_id = uuid.UUID(str(customer["id"]))
            # Merge ushauth profile into recipient_data; caller-supplied
            # fields take precedence only for "name" when already set.
            merged: dict[str, Any] = {
                "id": str(customer.get("id", "")),
                "name": customer.get("name", "") or full_name_hint,
                "phone_number": customer.get("phone_number", body.recipient_phone),
                "email": customer.get("email") or recipient_data.get("email", ""),
                "avatar": customer.get("avatar"),
            }
            recipient_data = merged
            logger.info(
                "recipient_resolved_via_ushauth",
                recipient_id=str(recipient_id),
                created=customer.get("created", False),
            )
        except Exception as exc:
            # Non-fatal: log and continue with whatever the caller supplied
            logger.warning(
                "ushauth_get_or_create_failed",
                phone=body.recipient_phone,
                error=str(exc),
            )

    svc = GiftVoucherService(session)
    try:
        voucher = await svc.create_voucher(
            service_id=body.service_id,
            service_data=body.service_data,
            branch_id=body.branch_id,
            branch_data=body.branch_data,
            service_arrangement_id=body.service_arrangement_id,
            service_arrangement_data=body.service_arrangement_data,
            addons=body.addons,
            extra_time=body.extra_time,
            price_for_extra_time=body.price_for_extra_time,
            total_duration=body.total_duration,
            total_amount=body.total_amount,
            currency=body.currency,
            sender_id=sender_id,
            sender_data=sender_data,
            recipient_phone=body.recipient_phone,
            recipient_id=recipient_id,
            recipient_data=recipient_data,
            gift_message=body.gift_message,
            gift_template=body.gift_template,
            booking_id=body.booking_id,
            created_by=sender_id,
            payment_url=body.payment_url,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc

    data = _voucher_to_response(voucher)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"success": True, "data": data.model_dump(mode="json")},
    )



@router.get(
    "/",
    summary="List all gift vouchers",
    description=(
        "Return all gift vouchers. Public endpoint requiring USH_TOKEN. "
        "Sends all vouchers in response without filtering by created_by or sender."
    ),
)
async def list_all_vouchers(
    _: RequireAppToken,
    session: DBSession,
    status_filter: str | None = Query(default=None, alias="status", description="Filter by voucher status."),
    sender_id: uuid.UUID | None = Query(default=None, description="Optional filter by sender ID."),
    service_id: uuid.UUID | None = Query(default=None, description="Optional filter by service ID."),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)."),
    page_size: int = Query(default=1000, ge=1, le=5000, description="Items per page (defaults to 1000 to send all vouchers)."),
) -> JSONResponse:
    """List all gift vouchers. Requires USH_TOKEN only, not filtered by created_by."""
    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 1000)
    p_status = _val(status_filter)
    p_sender_id = _val(sender_id)
    p_service_id = _val(service_id)

    svc = GiftVoucherService(session)
    vouchers, total = await svc.list_all(
        status=p_status,
        sender_id=p_sender_id,
        service_id=p_service_id,
        page=p_page,
        page_size=p_page_size,
    )
    items = [_voucher_to_response(v) for v in vouchers]
    paginated = make_paginated_response(
        items, count=total, page=p_page, page_size=p_page_size
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=paginated.model_dump(mode="json"),
    )


list_my_vouchers = list_all_vouchers


@router.get(
    "/my-vouchers/",
    summary="List my received gift vouchers",
    description=(
        "Return all gift vouchers where recipient mobile number matches requester mobile number. "
        "Supports filtering by status (e.g. 'active', 'redeemed', 'expired', 'available'), "
        "created_at date, from_date, to_date, service_id, branch_id, etc."
    ),
)
@router.get(
    "/my-vouchers",
    include_in_schema=False,
)
async def list_my_received_vouchers(
    current_user: CurrentUser,
    session: DBSession,
    request: Request,
    status_filter: str | None = Query(default=None, alias="status", description="Filter by voucher status (e.g. 'active', 'redeemed', 'expired', 'available')."),
    created_at: str | None = Query(default=None, description="Filter vouchers created on specific date (YYYY-MM-DD)."),
    from_date: str | None = Query(default=None, description="Filter vouchers created on or after date/datetime (YYYY-MM-DD or ISO)."),
    to_date: str | None = Query(default=None, description="Filter vouchers created on or before date/datetime (YYYY-MM-DD or ISO)."),
    service_id: uuid.UUID | None = Query(default=None, description="Filter by service UUID."),
    branch_id: uuid.UUID | None = Query(default=None, description="Filter by branch UUID."),
    available_only: bool = Query(default=False, description="If True, only return active and non-expired vouchers."),
    phone: str | None = Query(default=None, description="Optional phone number override if not found in JWT token."),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)."),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page."),
) -> JSONResponse:
    """List gift vouchers where recipient mobile number matches the current user's phone number."""
    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 20)
    p_status = _val(status_filter)
    p_created_at = _val(created_at)
    p_from_date = _val(from_date)
    p_to_date = _val(to_date)
    p_service_id = _val(service_id)
    p_branch_id = _val(branch_id)
    p_available_only = bool(_val(available_only, False))
    p_phone = _val(phone)

    requester_phone = getattr(current_user, "phone_number", None) or p_phone
    if not requester_phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Requester phone number is required and was not found in user token or parameters.",
        )

    svc = GiftVoucherService(session)
    vouchers, total = await svc.list_vouchers_for_recipient(
        recipient_phone=requester_phone,
        status=p_status,
        created_at=p_created_at,
        from_date=p_from_date,
        to_date=p_to_date,
        service_id=p_service_id,
        branch_id=p_branch_id,
        available_only=p_available_only,
        page=p_page,
        page_size=p_page_size,
    )
    items = [_voucher_to_response(v) for v in vouchers]
    base_url = str(request.url.remove_query_params(["page", "page_size"]))
    paginated = make_paginated_response(
        items, count=total, page=p_page, page_size=p_page_size, base_url=base_url
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=paginated.model_dump(mode="json"),
    )


@router.get(
    "/my-sent-vouchers/",
    summary="List my sent gift vouchers",
    description=(
        "Return all gift vouchers where the requester is the **sender** (i.e. vouchers "
        "I have purchased and given to others). "
        "Supports filtering by status (e.g. 'active', 'redeemed', 'expired', 'created', "
        "'payment_pending', 'cancelled'), date, service, and branch. "
        "Results are paginated newest-first."
    ),
)
@router.get(
    "/my-sent-vouchers",
    include_in_schema=False,
)
async def list_my_sent_vouchers(
    current_user: CurrentUser,
    session: DBSession,
    request: Request,
    status_filter: str | None = Query(default=None, alias="status", description="Filter by voucher status (e.g. 'active', 'redeemed', 'expired', 'created')."),
    created_at: str | None = Query(default=None, description="Filter vouchers created on a specific date (YYYY-MM-DD)."),
    from_date: str | None = Query(default=None, description="Filter vouchers created on or after this date/datetime (YYYY-MM-DD or ISO)."),
    to_date: str | None = Query(default=None, description="Filter vouchers created on or before this date/datetime (YYYY-MM-DD or ISO)."),
    service_id: uuid.UUID | None = Query(default=None, description="Filter by service UUID."),
    branch_id: uuid.UUID | None = Query(default=None, description="Filter by branch UUID."),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)."),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page."),
) -> JSONResponse:
    """
    List gift vouchers that the authenticated user sent (I am the sender).

    Uses sender_id from the JWT sub claim — no phone number needed.
    """
    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 20)
    p_status = _val(status_filter)
    p_created_at = _val(created_at)
    p_from_date = _val(from_date)
    p_to_date = _val(to_date)
    p_service_id = _val(service_id)
    p_branch_id = _val(branch_id)

    sender_id = uuid.UUID(current_user.sub)

    svc = GiftVoucherService(session)
    vouchers, total = await svc.list_sent_vouchers(
        sender_id=sender_id,
        status=p_status,
        created_at=p_created_at,
        from_date=p_from_date,
        to_date=p_to_date,
        service_id=p_service_id,
        branch_id=p_branch_id,
        page=p_page,
        page_size=p_page_size,
    )
    items = [_voucher_to_response(v) for v in vouchers]
    base_url = str(request.url.remove_query_params(["page", "page_size"]))
    paginated = make_paginated_response(
        items, count=total, page=p_page, page_size=p_page_size, base_url=base_url
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=paginated.model_dump(mode="json"),
    )


# ── Internal / admin endpoints ─────────────────────────────────────────────────


@router.get(
    "/admin/",
    summary="Admin: list all gift vouchers",
    description="Return a paginated, filterable list of all gift vouchers. Requires USHSPA-TOKEN.",
)
@router.get(
    "/admin",
    include_in_schema=False,
)
async def admin_list_vouchers(
    _: RequireAppToken,
    session: DBSession,
    status_filter: str | None = Query(default=None, alias="status"),
    sender_id: uuid.UUID | None = Query(default=None),
    service_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """Admin: list all vouchers with optional filters. Requires USHSPA-TOKEN."""
    svc = GiftVoucherService(session)
    vouchers, total = await svc.list_all(
        status=status_filter,
        sender_id=sender_id,
        service_id=service_id,
        page=page,
        page_size=page_size,
    )
    items = [_voucher_to_list_item(v) for v in vouchers]
    paginated = make_paginated_response(
        items, count=total, page=page, page_size=page_size
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=paginated.model_dump(mode="json"),
    )


# ── Public endpoint ────────────────────────────────────────────────────────────


@router.get(
    "/public/{public_token}/",
    summary="Public gift card page",
    description=(
        "Return public-facing gift voucher details for the shareable gift card URL. "
        "No authentication required. Secret code is NOT included in the response."
    ),
)
@router.get(
    "/public/{public_token}",
    include_in_schema=False,
)
async def public_voucher_page(
    public_token: str,
    session: DBSession,
) -> JSONResponse:
    """
    Public gift card page endpoint — no auth required, no secret_code in response.

    The secret_code is delivered to the recipient via ushnotice (SMS/Email/WhatsApp)
    when the voucher becomes active, not through this public URL.
    """
    svc = GiftVoucherService(session)
    try:
        voucher = await svc.get_by_public_token(public_token)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc

    data = _voucher_to_public(voucher)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "data": data.model_dump(mode="json")},
    )


# ── Voucher ID parameterized endpoints (defined after all static routes) ───────


@router.patch(
    "/{voucher_id}/status/",
    summary="Update voucher status (internal)",
    description=(
        "Advance a gift voucher's status. "
        "Called internally by the payment webhook (active/payment_pending) "
        "or the booking confirmation flow (redeemed). "
        "Also supports: cancelled, expired, fulfilled."
    ),
)
@router.patch(
    "/{voucher_id}/status",
    include_in_schema=False,
)
async def update_voucher_status(
    voucher_id: uuid.UUID,
    body: UpdateGiftVoucherStatusRequest,
    _: RequireAppToken,
    session: DBSession,
) -> JSONResponse:
    """Update a gift voucher's status. Requires USHSPA-TOKEN."""
    svc = GiftVoucherService(session)
    try:
        voucher = await svc.update_status(
            voucher_id,
            body.status,
            payment_id=body.payment_id,
            payment_data=body.payment_data,
            payment_url=body.payment_url,
            booking_id=body.booking_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc

    data = _voucher_to_response(voucher)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "data": data.model_dump(mode="json")},
    )


@router.get(
    "/{voucher_id}/",
    summary="Get gift voucher detail",
    description=(
        "Return full detail for a gift voucher. "
        "Only accessible by the sender (JWT) or an admin (app token)."
    ),
)
@router.get(
    "/{voucher_id}",
    include_in_schema=False,
)
async def get_voucher_detail(
    voucher_id: uuid.UUID,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Return full voucher detail for the sender."""
    svc = GiftVoucherService(session)
    try:
        voucher = await svc.get_by_id(voucher_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc

    # Enforce ownership: only the sender can view their voucher via this JWT endpoint
    sender_id = uuid.UUID(current_user.sub)
    if voucher.sender_id != sender_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this voucher.",
        )

    data = _voucher_to_response(voucher)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "data": data.model_dump(mode="json")},
    )
