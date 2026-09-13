"""
app/shop/api/router.py
────────────────────────
FastAPI router for the Shop module.

Endpoints (Kong strips /booknpay, so full public URL = /booknpay/api/v1/...):

  Staff / Delivery Agent (JWT-authenticated):
    GET    /api/v1/orders/                    — list all orders
    GET    /api/v1/orders/{order_id}/         — order detail
    PATCH  /api/v1/orders/{order_id}/status/  — update delivery status
    PATCH  /api/v1/orders/{order_id}/payment/ — update payment status

  Customer (JWT-authenticated):
    POST   /api/v1/orders/                    — place a new order
    GET    /api/v1/my-orders/                 — my orders
    GET    /api/v1/my-orders/{order_id}/      — my order detail

  Public (no auth):
    GET    /api/v1/track/{order_number}/           — public tracking
    POST   /api/v1/track/{order_number}/received/  — confirm received

  Product catalogue (no auth, live from ushauth):
    GET    /api/v1/products/                  — list active products
    GET    /api/v1/products/{product_id}/     — product detail
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.exceptions import (
    AuthorizationError,
    NotFoundError,
    ValidationError,
)
from app.core.security import TokenPayload, require_authenticated_user
from app.shop.application.services import ShopOrderService
from app.shop.domain.value_objects import DeliveryStatus, OrderPaymentStatus
from app.shop.infrastructure.repository import ShopOrderNotFoundError
from app.shop.interfaces.schemas import (
    ConfirmReceivedRequest,
    CreateShopOrderRequest,
    PublicOrderTrackingResponse,
    ShopOrderDetailResponse,
    ShopOrderListResponse,
    UpdateDeliveryStatusRequest,
    UpdatePaymentStatusRequest,
    order_to_detail,
    order_to_list_item,
    order_to_public_tracking,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Shop Orders"])

# ── Dependencies ──────────────────────────────────────────────────────────────

from app.api.deps import USHAuthDep, get_http_client
from app.integrations.ushauth_client import USHAuthClient

Session = Annotated[AsyncSession, Depends(get_db_session)]
CurrentUser = Annotated[TokenPayload, Depends(require_authenticated_user)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _service(
    session: Session,
    settings: AppSettings,
    ushauth: USHAuthDep,
) -> ShopOrderService:
    return ShopOrderService(session=session, settings=settings, ushauth_client=ushauth)


def _require_staff(current_user: CurrentUser) -> TokenPayload:
    """Ensure the caller is staff (not a plain customer)."""
    if current_user.user_type not in ("staff", "admin", "superuser"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff access required.",
        )
    return current_user


# ── Staff / Delivery Agent endpoints ─────────────────────────────────────────


@router.get(
    "/orders/",
    response_model=ShopOrderListResponse,
    summary="List all orders (staff)",
    description="Paginated list of all shop orders with optional filters. Requires staff JWT.",
)
async def list_orders(
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
    delivery_status: str | None = Query(default=None, description="Filter by delivery status"),
    payment_status: str | None = Query(default=None, description="Filter by payment status"),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    search: str | None = Query(default=None, description="Search by order number, name, or phone"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ShopOrderListResponse:
    _require_staff(current_user)
    orders, total = await svc.list_orders(
        delivery_status=delivery_status,
        payment_status=payment_status,
        from_date=from_date,
        to_date=to_date,
        search=search,
        limit=limit,
        offset=offset,
    )
    return ShopOrderListResponse(
        total=total,
        limit=limit,
        offset=offset,
        results=[order_to_list_item(o) for o in orders],
    )


@router.get(
    "/orders/{order_id}/",
    response_model=ShopOrderDetailResponse,
    summary="Order detail (staff or order owner)",
    description=(
        "Returns full order detail. "
        "Staff (admin/superuser/staff) can view any order. "
        "Customers can only view their own orders."
    ),
)
async def get_order(
    order_id: uuid.UUID,
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> ShopOrderDetailResponse:
    try:
        order = await svc.get_order(order_id)
    except (NotFoundError, ShopOrderNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    is_staff = current_user.user_type in ("staff", "admin", "superuser")
    is_owner = str(order.customer_id) == str(current_user.sub)

    if not is_staff and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this order.",
        )

    return order_to_detail(order)


@router.patch(
    "/orders/{order_id}/status/",
    response_model=ShopOrderDetailResponse,
    summary="Update delivery status (staff/delivery agent)",
    description=(
        "Advance the order delivery status. "
        "Valid transitions: ordered → ready_to_go → on_the_way → delivered. "
        "Only the customer can set 'received' via the public tracking URL."
    ),
)
async def update_delivery_status(
    order_id: uuid.UUID,
    body: UpdateDeliveryStatusRequest,
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> ShopOrderDetailResponse:
    _require_staff(current_user)
    changed_by = f"staff:{current_user.sub}"
    try:
        order = await svc.update_delivery_status(
            order_id=order_id,
            new_status=body.status,
            changed_by=changed_by,
            note=body.note,
        )
    except (NotFoundError, ShopOrderNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        )
    return order_to_detail(order)


@router.patch(
    "/orders/{order_id}/payment/",
    response_model=ShopOrderDetailResponse,
    summary="Update payment status (staff/ushdesk or mobile)",
    description=(
        "Mark an order as paid (ushdesk cash), pending, or failed (mobile gateway). "
        "No payment gateway integration — pure status update."
    ),
)
async def update_payment_status(
    order_id: uuid.UUID,
    body: UpdatePaymentStatusRequest,
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> ShopOrderDetailResponse:
    changed_by = (
        f"staff:{current_user.sub}"
        if current_user.user_type in ("staff", "admin", "superuser")
        else f"customer:{current_user.sub}"
    )
    try:
        order = await svc.update_payment_status(
            order_id=order_id,
            new_payment_status=body.payment_status,
            changed_by=changed_by,
        )
    except (NotFoundError, ShopOrderNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return order_to_detail(order)


# ── Customer endpoints (JWT-authenticated) ────────────────────────────────────


@router.post(
    "/orders/",
    response_model=ShopOrderDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Place a new order (customer or staff on behalf)",
    description=(
        "Create a shop order. Products are validated and prices are snapshotted "
        "from ushauth at creation time. A ShopOrderCreated SQS event is fired "
        "so ushnotice can send the confirmation message with tracking URL."
    ),
)
async def create_order(
    body: CreateShopOrderRequest,
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> ShopOrderDetailResponse:
    customer_id = uuid.UUID(current_user.sub)
    customer_name = " ".join(
        filter(None, [current_user.first_name, current_user.last_name])
    ) or ""
    # Use JWT phone first; fall back to contact_number in request body
    customer_phone = (
        current_user.phone_number
        or body.contact_number
        or ""
    )

    try:
        order = await svc.create_order(
            body=body,
            customer_id=customer_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        )
    return order_to_detail(order)


@router.get(
    "/my-orders/",
    response_model=ShopOrderListResponse,
    summary="List my orders (customer)",
)
async def list_my_orders(
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ShopOrderListResponse:
    customer_id = uuid.UUID(current_user.sub)
    orders, total = await svc.get_my_orders(customer_id, limit=limit, offset=offset)
    return ShopOrderListResponse(
        total=total,
        limit=limit,
        offset=offset,
        results=[order_to_list_item(o) for o in orders],
    )


@router.get(
    "/my-orders/{order_id}/",
    response_model=ShopOrderDetailResponse,
    summary="My order detail (customer)",
)
async def get_my_order(
    order_id: uuid.UUID,
    current_user: CurrentUser,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> ShopOrderDetailResponse:
    customer_id = uuid.UUID(current_user.sub)
    try:
        order = await svc.get_order(order_id)
    except (NotFoundError, ShopOrderNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if order.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return order_to_detail(order)


# ── Public tracking endpoints (no auth) ───────────────────────────────────────


@router.get(
    "/track/{order_number}/",
    response_model=PublicOrderTrackingResponse,
    summary="Public order tracking",
    description=(
        "Returns order status and history without any PII. "
        "Accessible without authentication — share this URL with the customer."
    ),
)
async def track_order(
    order_number: str,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> PublicOrderTrackingResponse:
    try:
        order = await svc.get_order_by_number(order_number)
    except (NotFoundError, ShopOrderNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return order_to_public_tracking(order)


@router.post(
    "/track/{order_number}/received/",
    response_model=PublicOrderTrackingResponse,
    summary="Customer confirms receipt",
    description=(
        "Customer visits the public tracking URL after delivery and confirms receipt. "
        "Requires the secret tracking_code sent to them via SMS/WhatsApp. "
        "Only valid when current status is 'delivered'."
    ),
)
async def confirm_received(
    order_number: str,
    body: ConfirmReceivedRequest,
    svc: Annotated[ShopOrderService, Depends(_service)],
) -> PublicOrderTrackingResponse:
    try:
        order = await svc.confirm_received(
            order_number=order_number,
            tracking_code=body.tracking_code,
        )
    except (NotFoundError, ShopOrderNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        )
    return order_to_public_tracking(order)


# ── Product catalogue proxy (no auth, cached) ─────────────────────────────────


@router.get(
    "/products/",
    summary="List active products (from ushauth)",
    response_model=list[dict],
)
async def list_products(
    ushauth: USHAuthDep,
    category_id: str | None = Query(default=None),
    featured: bool | None = Query(default=None),
) -> list[dict]:
    products = await ushauth.list_products(
        category_id=category_id,
        featured=featured,
    )
    return products


@router.get(
    "/products/{product_id}/",
    summary="Product detail (from ushauth)",
    response_model=dict,
)
async def get_product(
    product_id: str,
    ushauth: USHAuthDep,
) -> dict:
    try:
        return await ushauth.get_product(product_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found.",
        )
