"""
app/voucher/api/router.py
──────────────────────────
Gift Voucher API endpoints.

Routes (all under /api/v1/vouchers/):

  Public / Interservice (require USHSPA-TOKEN):
    GET    /                                — List all gift vouchers (filterable by status, delivery_status, gift_category, expire_date, created_at, payment_through, sender_id, service_id)

  Customer-facing (require JWT Bearer):
    POST   /                                — Create a gift voucher (supports digital, physical, service categories; delivery)
    GET    /{voucher_id}/                   — Get full voucher detail (sender or admin only)
    GET    /my-vouchers/                    — List my received vouchers (I am recipient)
    GET    /my-sent-vouchers/               — List my sent vouchers (I am sender)

  Internal / Staff (require USHSPA-TOKEN):
    PATCH  /{voucher_id}/                   — Update gift voucher details (partial)
    PUT    /{voucher_id}/                   — Update gift voucher details (full/partial)
    PATCH  /{voucher_id}/status/            — Update voucher status (payment webhook / booking)
    PATCH  /{voucher_id}/delivery-status/   — Update voucher delivery status (ordered → ready_to_go → on_the_way → delivered → received)
    GET    /admin/                          — Admin: list all vouchers (paginated, filterable)

  Public (no auth):
    GET    /public/{public_token}/          — Public gift card page (no secret_code exposed)
    POST   /public/{public_token}/          — Verify secret_code and return full voucher details
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings, CurrentUser, DBSession, RequireAppToken
from app.clients import ushauth as ushauth_client
from app.common.pagination import make_paginated_response
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import (
    _get_ushspa_token,
    validate_user_with_ushauth,
    verify_ushspa_token,
)
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import DeliveryStatus, get_delivery_status_label
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.interfaces.schemas import (
    CreateGiftVoucherRequest,
    GiftVoucherListItem,
    GiftVoucherPublicResponse,
    GiftVoucherResponse,
    UpdateGiftVoucherRequest,
    UpdateGiftVoucherStatusRequest,
    UpdateVoucherDeliveryStatusRequest,
    VerifyVoucherSecretCodeRequest,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/vouchers", tags=["Gift Vouchers"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_gift_category(v: Any) -> str:
    cat = getattr(v, "gift_category", None)
    return cat if isinstance(cat, str) and cat else "service"


def _extract_ordered_items(v: Any) -> list[dict[str, Any]] | None:
    items = getattr(v, "ordered_items", None)
    return items if isinstance(items, list) else None


def _extract_delivery_status(v: Any) -> str | None:
    ds = getattr(v, "delivery_status", None)
    if isinstance(ds, DeliveryStatus):
        return ds.value
    return ds if isinstance(ds, str) and ds else None


def _extract_delivery_address(v: Any) -> dict[str, Any] | None:
    addr = getattr(v, "delivery_address", None)
    return addr if isinstance(addr, dict) else None


def _delivery_labels(ds_str: str | None) -> tuple[str | None, str | None]:
    if not ds_str or not isinstance(ds_str, str):
        return None, None
    try:
        ds = DeliveryStatus(ds_str)
        return get_delivery_status_label(ds, "en"), get_delivery_status_label(ds, "ar")
    except Exception:
        return ds_str, ds_str


def _voucher_to_response(v: GiftVoucher) -> GiftVoucherResponse:
    """Map ORM GiftVoucher → full GiftVoucherResponse."""
    delivery_status = _extract_delivery_status(v)
    label_en, label_ar = _delivery_labels(delivery_status)
    return GiftVoucherResponse(
        id=v.id,
        gift_category=_extract_gift_category(v),
        ordered_items=_extract_ordered_items(v),
        delivery_status=delivery_status,
        delivery_status_label=label_en,
        delivery_status_label_ar=label_ar,
        delivery_address=_extract_delivery_address(v),
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
        redeemed_by=v.redeemed_by,
        booking_id=v.booking_id,
        booking_data=v.booking_data or {},
        payment_id=v.payment_id,
        payment_data=v.payment_data,
        payment_url=v.payment_url,
        payment_provider=v.payment_provider,
        payment_through=v.payment_through,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def _voucher_to_public(v: GiftVoucher) -> GiftVoucherPublicResponse:
    """Map ORM GiftVoucher → public (no secret_code) response."""
    # Only expose sender's name on the public page, not phone number
    sender_public: dict[str, Any] = {"name": (v.sender_data or {}).get("name", "")}
    delivery_status = _extract_delivery_status(v)
    label_en, label_ar = _delivery_labels(delivery_status)
    return GiftVoucherPublicResponse(
        id=v.id,
        gift_category=_extract_gift_category(v),
        ordered_items=_extract_ordered_items(v),
        delivery_status=delivery_status,
        delivery_status_label=label_en,
        delivery_status_label_ar=label_ar,
        delivery_address=_extract_delivery_address(v),
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
    delivery_status = _extract_delivery_status(v)
    label_en, label_ar = _delivery_labels(delivery_status)
    return GiftVoucherListItem(
        id=v.id,
        gift_category=_extract_gift_category(v),
        ordered_items=_extract_ordered_items(v),
        delivery_status=delivery_status,
        delivery_status_label=label_en,
        delivery_status_label_ar=label_ar,
        delivery_address=_extract_delivery_address(v),
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
        redeemed_by=v.redeemed_by,
        booking_id=v.booking_id,
        booking_data=v.booking_data or {},
        payment_id=v.payment_id,
        payment_data=v.payment_data,
        payment_url=v.payment_url,
        payment_provider=v.payment_provider,
        payment_through=v.payment_through,
        created_by=v.created_by,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


# ── Customer endpoints ─────────────────────────────────────────────────────────


@router.post(
    "/",
    summary="Create a gift voucher",
    description=(
        "Create a new gift voucher for a service, physical gift, or digital gift package.\n\n"
        "### Key Parameters:\n"
        "- **service_id** (UUID, optional): The external UUID of the spa service being gifted.\n"
        "- **total_amount** (Decimal/String, required): Total price of the voucher in KWD.\n"
        "- **gift_category** (string, optional): Category of voucher: `digital`, `physical`, `service`. Defaults to `service`.\n"
        "- **ordered_items** (list[dict], optional): Optional itemized list for product/service combinations.\n"
        "- **delivery_status** (string, optional): Delivery state (`ordered`, `ready_to_go`, `on_the_way`, `delivered`, `received`).\n"
        "- **delivery_address** (dict, optional): Structured delivery address (city, street, block, avenue, etc.).\n"
        "- **sender_id** (UUID, optional): UUID of the customer purchasing/sending the voucher. If omitted, defaults to the authenticated user from the JWT token.\n"
        "- **sender_data** (object, optional): Snapshot of sender contact details (`name`, `phone_number`). Defaults to authenticated customer profile if empty.\n"
        "- **recipient_phone** (string, optional): Recipient mobile number (e.g. `+965...`) for automated SMS/WhatsApp delivery of credentials and secret code.\n"
        "- **recipient_id** (UUID, optional): Recipient customer UUID in ushauth (automatically resolved or created if `recipient_phone` is provided; if newly created, auto-generates 6-digit login password).\n"
        "- **recipient_data** (object, optional): Snapshot of recipient details (`name`, `email`, `phone_number`).\n"
        "- **status** (string, optional): Initial voucher status (`created`, `payment_pending`, `active`). Defaults to `created`. If created as `active`, triggers recipient notification immediately.\n"
        "- **payment_id** (string, optional): Payment gateway transaction/invoice ID (e.g. `100624710000000255`).\n"
        "- **payment_data** (object, optional): Full gateway provider response snapshot (JSONB) stored for complete audit records.\n"
        "- **payment_url** (string, optional): Payment gateway hosted checkout/redirect URL.\n"
        "- **payment_provider** (string, optional): Payment gateway: `MyFatoorah`, `DirectLink`, `Deema`, `Other`.\n"
        "- **payment_through** (string, optional): Channel through which the voucher was sold: `ushspa` (app/web), `desk` (reception/front desk).\n"
        "- **branch_id** / **branch_data** (optional): Branch where the service will be rendered.\n"
        "- **service_arrangement_id** / **service_arrangement_data** (optional): Room/package arrangement.\n"
        "- **addons** (list, optional): Add-on snapshots `[{'addon_id': ..., 'name': ..., 'price': ..., 'duration': ...}]`.\n"
        "- **extra_time** / **price_for_extra_time** (optional): Extra service minutes and unit price.\n"
        "- **gift_message** (string, optional): Personalised gift greeting displayed on the gift card.\n"
        "- **gift_template** (string, optional): Visual card template theme identifier.\n"
        "- **expire_date** (datetime, optional): Explicit expiry date/time. Defaults to +60 days.\n"
        "- **booking_id** / **booking_data** (optional): Triggering booking reference.\n"
        "- **created_by** (UUID, optional): Staff or admin identifier who authored the voucher.\n"
    ),
    status_code=status.HTTP_201_CREATED,
)
async def create_gift_voucher(
    body: CreateGiftVoucherRequest,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Create a new gift voucher for the authenticated customer."""
    # created_by is automatically set to the authenticated API requester
    created_by = uuid.UUID(current_user.sub)
    sender_id = body.sender_id or created_by
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
            if customer.get("password"):
                merged["password"] = customer["password"]
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
            booking_data=body.booking_data,
            created_by=created_by,
            payment_id=body.payment_id,
            payment_data=body.payment_data,
            payment_url=body.payment_url,
            payment_provider=body.payment_provider,
            payment_through=body.payment_through,
            status=body.status,
            expire_date=body.expire_date,
            gift_category=body.gift_category,
            ordered_items=body.ordered_items,
            delivery_status=body.delivery_status,
            delivery_address=body.delivery_address,
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
    delivery_status: str | None = Query(default=None, description="Optional filter by delivery status (e.g. 'ordered', 'ready_to_go', 'on_the_way', 'delivered', 'received')."),
    gift_category: str | None = Query(default=None, description="Optional filter by gift category (e.g. 'service', 'digital', 'physical')."),
    expire_date: str | None = Query(default=None, description="Optional filter by expiry date (YYYY-MM-DD or ISO datetime)."),
    created_at: str | None = Query(default=None, description="Optional filter by creation date (YYYY-MM-DD or ISO datetime)."),
    payment_through: str | None = Query(default=None, description="Optional filter by sales channel / payment through (e.g. 'ushspa', 'desk')."),
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
    p_delivery_status = _val(delivery_status)
    p_gift_category = _val(gift_category)
    p_expire_date = _val(expire_date)
    p_created_at = _val(created_at)
    p_payment_through = _val(payment_through)
    p_sender_id = _val(sender_id)
    p_service_id = _val(service_id)

    svc = GiftVoucherService(session)
    list_kwargs: dict[str, Any] = {
        "status": p_status,
        "sender_id": p_sender_id,
        "service_id": p_service_id,
        "page": p_page,
        "page_size": p_page_size,
    }
    if p_delivery_status is not None:
        list_kwargs["delivery_status"] = p_delivery_status
    if p_gift_category is not None:
        list_kwargs["gift_category"] = p_gift_category
    if p_expire_date is not None:
        list_kwargs["expire_date"] = p_expire_date
    if p_created_at is not None:
        list_kwargs["created_at"] = p_created_at
    if p_payment_through is not None:
        list_kwargs["payment_through"] = p_payment_through

    vouchers, total = await svc.list_all(**list_kwargs)
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
    delivery_status: str | None = Query(default=None, description="Filter by delivery status"),
    gift_category: str | None = Query(default=None, description="Filter by gift category"),
    expire_date: str | None = Query(default=None, description="Filter by expiry date (YYYY-MM-DD or ISO)"),
    created_at: str | None = Query(default=None, description="Filter by created date (YYYY-MM-DD or ISO)"),
    payment_through: str | None = Query(default=None, description="Filter by sales channel / payment through"),
    sender_id: uuid.UUID | None = Query(default=None),
    service_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """Admin: list all vouchers with optional filters. Requires USHSPA-TOKEN."""
    svc = GiftVoucherService(session)
    vouchers, total = await svc.list_all(
        status=status_filter,
        delivery_status=delivery_status,
        gift_category=gift_category,
        expire_date=expire_date,
        created_at=created_at,
        payment_through=payment_through,
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


@router.post(
    "/public/{public_token}/",
    summary="Verify secret code and get voucher details",
    description=(
        "Send secret_code as payload to verify against the voucher with public_token. "
        "If secret_code and public token match, returns the full voucher details."
    ),
    response_model=dict[str, Any],
)
@router.post(
    "/public/{public_token}",
    include_in_schema=False,
)
async def verify_public_voucher(
    public_token: str,
    payload: VerifyVoucherSecretCodeRequest,
    session: DBSession,
) -> JSONResponse:
    """
    Public endpoint to verify secret_code against public_token.
    Returns the full voucher details if verified.
    """
    svc = GiftVoucherService(session)
    try:
        voucher = await svc.verify_secret_code(public_token, payload.secret_code)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    data = _voucher_to_response(voucher)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "data": data.model_dump(mode="json")},
    )


# ── Voucher ID parameterized endpoints (defined after all static routes) ───────


def _resolve_api_requester(request: Request, explicit_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """
    Resolve the User UUID of the API requester.

    Checks:
    1. Explicit ID passed in request body
    2. Custom requester headers (X-User-Id, X-Customer-Id, etc.)
    3. JWT Bearer token claims in Authorization header
    """
    if explicit_id is not None:
        return explicit_id

    for header in (
        "X-User-Id",
        "x-user-id",
        "X-Requester-Id",
        "x-requester-id",
        "X-Customer-Id",
        "X-Employee-Id",
        "x-employee-id",
    ):
        val = request.headers.get(header)
        if val:
            try:
                return uuid.UUID(val)
            except ValueError:
                pass

    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        try:
            import base64
            import json

            parts = token.split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                jwt_claims = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
                sub = jwt_claims.get("sub") or jwt_claims.get("user_id") or jwt_claims.get("customer_id")
                if sub:
                    return uuid.UUID(str(sub))
        except Exception:
            pass

    return None


@router.patch(
    "/{voucher_id}/status/",
    summary="Update voucher status (internal)",
    description=(
        "Advance a gift voucher's status. "
        "Called internally by the payment webhook (active/payment_pending) "
        "or the booking confirmation flow (redeemed). "
        "Also supports: cancelled, expired, fulfilled. "
        "When status is changed to 'redeemed', redeemed_at and redeemed_by are automatically updated."
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
    request: Request,
) -> JSONResponse:
    """Update a gift voucher's status. Requires USHSPA-TOKEN."""
    svc = GiftVoucherService(session)

    redeemed_by = None
    if body.status == "redeemed":
        redeemed_by = _resolve_api_requester(request, body.redeemed_by)
        if redeemed_by is None and body.booking_id is not None:
            try:
                from app.booking.infrastructure.models import Booking
                booking = await session.get(Booking, body.booking_id)
                if booking and getattr(booking, "customer_id", None):
                    redeemed_by = booking.customer_id
            except Exception:
                pass

    try:
        voucher = await svc.update_status(
            voucher_id,
            body.status,
            payment_id=body.payment_id,
            payment_data=body.payment_data,
            payment_url=body.payment_url,
            booking_id=body.booking_id,
            booking_data=body.booking_data,
            payment_provider=body.payment_provider,
            payment_through=body.payment_through,
            redeemed_by=redeemed_by,
            ordered_items=body.ordered_items,
            delivery_status=body.delivery_status,
            delivery_address=body.delivery_address,
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


STATUS_UPDATE_PERMISSIONS = frozenset({
    "status_update",
    "update_status",
    "delivery_status",
    "update_delivery_status",
    "voucher.update_status",
    "voucher.delivery_status",
    "vouchers.delivery_status",
    "vouchers:update",
    "vouchers.*",
    "*",
})


def _has_status_update_permission(
    user_type: str | None,
    permissions: Any = None,
    role: str | None = None,
    is_superuser: bool = False,
    is_staff: bool = False,
) -> bool:
    """Evaluate whether user attributes grant status update permission."""
    u_type = (user_type or "").strip().lower()
    if is_superuser or u_type in ("admin", "superuser"):
        return True

    if u_type in ("employee", "staff") or is_staff:
        # Check permissions if explicitly provided
        if permissions is not None:
            perms: set[str] = set()
            if isinstance(permissions, (list, tuple, set)):
                perms = {str(p).strip().lower() for p in permissions}
            elif isinstance(permissions, str):
                perms = {p.strip().lower() for p in permissions.split(",") if p.strip()}
            elif isinstance(permissions, dict):
                if (
                    permissions.get("status_update")
                    or permissions.get("delivery_status")
                    or permissions.get("can_update_status")
                    or permissions.get("update_delivery_status")
                ):
                    return True
                perms = {str(k).strip().lower() for k, v in permissions.items() if v}

            if perms:
                return bool(perms & STATUS_UPDATE_PERMISSIONS)
            return False

        # If role is specified, check if it's restricted (e.g. Therapist)
        if role:
            r = str(role).strip().lower()
            if "therapist" in r:
                return False

        # Default employee/staff has status update permission
        return True

    return False


async def _check_employee_status_update_permission(
    request: Request,
    settings: Settings,
) -> tuple[bool, str | None]:
    """
    Check whether the request is from an employee with status update permission.

    Returns:
        (is_authorized, detail_or_actor_id)
    """
    user_type: str | None = None
    permissions: Any = None
    role: str | None = None
    is_superuser: bool = False
    is_staff: bool = False
    sub: str | None = None

    # 1. Check custom headers (common from API gateway or internal callers)
    for h in ("X-User-Type", "x-user-type"):
        val = request.headers.get(h)
        if val:
            user_type = val
            break

    for h in ("X-Permissions", "x-permissions", "X-Employee-Permissions", "x-employee-permissions"):
        val = request.headers.get(h)
        if val:
            permissions = val
            break

    for h in ("X-Role", "x-role", "X-Employee-Role", "x-employee-role"):
        val = request.headers.get(h)
        if val:
            role = val
            break

    for h in ("X-Employee-Id", "x-employee-id", "X-User-Id", "x-user-id"):
        val = request.headers.get(h)
        if val:
            sub = val
            break

    # 2. Check Authorization Bearer token
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

        # Extract claims directly from JWT
        try:
            import base64
            import json

            parts = token.split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                jwt_claims = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
                if not user_type:
                    user_type = jwt_claims.get("user_type")
                if permissions is None:
                    permissions = jwt_claims.get("permissions")
                if not role:
                    role = jwt_claims.get("role") or jwt_claims.get("role_name")
                if not sub:
                    sub = str(jwt_claims.get("sub") or jwt_claims.get("user_id") or jwt_claims.get("id") or "")
                if "is_superuser" in jwt_claims:
                    is_superuser = bool(jwt_claims.get("is_superuser"))
                if "is_staff" in jwt_claims:
                    is_staff = bool(jwt_claims.get("is_staff"))
        except Exception:
            pass

        # Also attempt validation via ushauth / redis cache if available
        try:
            user_payload = await validate_user_with_ushauth(token, settings)
            if user_payload:
                if user_payload.user_type:
                    user_type = user_payload.user_type
                if getattr(user_payload, "permissions", None) is not None:
                    permissions = user_payload.permissions
                if getattr(user_payload, "role", None):
                    role = user_payload.role
                if getattr(user_payload, "is_superuser", None) is not None:
                    is_superuser = bool(user_payload.is_superuser)
                if getattr(user_payload, "is_staff", None) is not None:
                    is_staff = bool(user_payload.is_staff)
                if user_payload.sub:
                    sub = user_payload.sub
        except Exception as exc:
            logger.debug("ushauth_validation_skipped_in_delivery_check", error=str(exc))

    # Evaluate permission
    u_norm = (user_type or "").strip().lower()
    if u_norm in ("employee", "staff", "admin", "superuser") or is_staff or is_superuser:
        if _has_status_update_permission(
            user_type=user_type,
            permissions=permissions,
            role=role,
            is_superuser=is_superuser,
            is_staff=is_staff,
        ):
            actor_id = sub or "employee"
            return True, actor_id
        return False, "employee_missing_permission"

    return False, "not_an_employee"


@router.patch(
    "/{voucher_id}/delivery-status/",
    summary="Update voucher delivery status (employee or public with secret code)",
    description=(
        "Advance the voucher's delivery status. "
        "Valid transitions: ordered → ready_to_go → on_the_way → delivered → received. "
        "Allowed for employees with status update permission, or public requests with secret_code in body and X-USHSPA-TOKEN in header."
    ),
)
@router.patch(
    "/{voucher_id}/delivery-status",
    include_in_schema=False,
)
async def update_voucher_delivery_status(
    voucher_id: uuid.UUID,
    body: UpdateVoucherDeliveryStatusRequest,
    session: DBSession,
    request: Request,
    settings: AppSettings = None,
) -> JSONResponse:
    """
    Update a gift voucher's delivery status.

    Allowed for:
    1. Employee with status update permission (via Bearer JWT or employee headers).
    2. Public request providing secret_code in request body and valid X-USHSPA-TOKEN in header.
    """
    if settings is None or not (isinstance(settings, Settings) or hasattr(settings, "USHSPA_TOKEN")):
        settings = get_settings()

    svc = GiftVoucherService(session)

    # 1. Check if requester is an employee with status update permission
    is_employee, employee_info = await _check_employee_status_update_permission(request, settings)

    if is_employee:
        changed_by = f"employee:{employee_info}" if employee_info else "employee"
    else:
        # 2. Not authorized as employee with permission — evaluate public request path
        # Public request requires valid application token (X-USHSPA-TOKEN / USH_TOKEN)
        try:
            app_token = _get_ushspa_token(request)
            verify_ushspa_token(app_token, settings)
        except (HTTPException, AuthorizationError):
            if employee_info == "employee_missing_permission":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Employee does not have status update permission.",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="USH_TOKEN / X-USHSPA-TOKEN header is required.",
            )

        # Public request requires secret_code in request body
        if not body.secret_code or not body.secret_code.strip():
            if employee_info == "employee_missing_permission":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Employee does not have status update permission.",
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="secret_code is required in request body for public delivery status update.",
            )

        # Fetch voucher to verify secret_code
        try:
            voucher = await svc.get_by_id(voucher_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc

        if not voucher.secret_code or voucher.secret_code.strip() != body.secret_code.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid secret code.",
            )

        actor_id = _resolve_api_requester(request)
        changed_by = f"public:{actor_id}" if actor_id else "public:secret_code"

    # Advance delivery status
    try:
        voucher = await svc.update_delivery_status(
            voucher_id=voucher_id,
            new_status=body.status,
            changed_by=changed_by,
            note=body.note,
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


@router.patch(
    "/{voucher_id}/",
    summary="Update gift voucher (partial)",
    description=(
        "Update an existing gift voucher. Supports partial updates for service, "
        "branch, arrangements, addons, extra time, amounts, sender/recipient data, "
        "gift message, delivery, payment details, and status. Requires USHSPA-TOKEN."
    ),
    response_model=dict[str, Any],
)
@router.patch(
    "/{voucher_id}",
    include_in_schema=False,
)
@router.put(
    "/{voucher_id}/",
    summary="Update gift voucher (full/partial)",
    description="Update gift voucher details using REST PUT. Requires USHSPA-TOKEN.",
    response_model=dict[str, Any],
)
@router.put(
    "/{voucher_id}",
    include_in_schema=False,
)
async def update_gift_voucher(
    voucher_id: uuid.UUID,
    body: UpdateGiftVoucherRequest,
    _: RequireAppToken,
    session: DBSession,
    request: Request,
) -> JSONResponse:
    """Update a gift voucher resource. Requires USHSPA-TOKEN."""
    updates = body.model_dump(exclude_unset=True)
    actor_id = _resolve_api_requester(request)

    # ── Auto-resolve redeemed_by if transitioning to redeemed ────────────
    if updates.get("status") == "redeemed" and updates.get("redeemed_by") is None:
        redeemed_by = _resolve_api_requester(request, body.redeemed_by)
        booking_ref = updates.get("booking_id") or body.booking_id
        if redeemed_by is None and booking_ref is not None:
            try:
                from app.booking.infrastructure.models import Booking
                booking = await session.get(Booking, booking_ref)
                if booking and getattr(booking, "customer_id", None):
                    redeemed_by = booking.customer_id
            except Exception:
                pass
        if redeemed_by is not None:
            updates["redeemed_by"] = redeemed_by

    # ── Auto-resolve recipient via ushauth if recipient_phone updated ────
    if updates.get("recipient_phone") and not updates.get("recipient_id"):
        try:
            settings = get_settings()
            rec_data = updates.get("recipient_data") or {}
            full_name_hint = rec_data.get("name", "") if isinstance(rec_data, dict) else ""
            customer = await ushauth_client.get_or_create_customer(
                phone_number=updates["recipient_phone"],
                full_name=full_name_hint,
                settings=settings,
            )
            if customer.get("id"):
                updates["recipient_id"] = uuid.UUID(str(customer["id"]))
            if isinstance(rec_data, dict):
                merged = {
                    "id": str(customer.get("id", "")),
                    "name": customer.get("name", "") or full_name_hint,
                    "phone_number": customer.get("phone_number", updates["recipient_phone"]),
                    "email": customer.get("email") or rec_data.get("email", ""),
                    "avatar": customer.get("avatar"),
                }
                if customer.get("password"):
                    merged["password"] = customer["password"]
                updates["recipient_data"] = merged
        except Exception as exc:
            logger.warning(
                "ushauth_get_or_create_failed_on_update",
                phone=updates.get("recipient_phone"),
                error=str(exc),
            )

    svc = GiftVoucherService(session)
    try:
        voucher = await svc.update_voucher(
            voucher_id=voucher_id,
            fields_to_update=updates,
            actor_id=str(actor_id) if actor_id else None,
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

