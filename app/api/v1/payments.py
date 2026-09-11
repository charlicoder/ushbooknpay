"""
app/api/v1/payments.py
───────────────────────
Payment API endpoints for creating, listing, retrieving, updating, and deleting payments.

Routes:
  POST   /api/v1/payments/                    → Create payment / Ingest gateway response
  GET    /api/v1/payments/                    → List payments (paginated, filters, finance summary)
  GET    /api/v1/payments/{payment_id}/       → Get payment detail
  PATCH  /api/v1/payments/{payment_id}/       → Update payment (partial)
  PUT    /api/v1/payments/{payment_id}/       → Update payment (full)
  DELETE /api/v1/payments/{payment_id}/       → Delete payment
  POST   /api/v1/payments/initiate/           → Initiate payment session
  GET    /api/v1/payments/{booking_id}/status/ → Get payment status for booking
  POST   /api/v1/payments/webhook/myfatoorah/ → MyFatoorah webhook
  POST   /api/v1/payments/webhook/tap/        → Tap webhook
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, or_, select

from app.api.deps import AppSettings, CurrentUser, DBSession, RedisClient, RequireAppToken
from app.booking.application.services import BookingService
from app.booking.domain.value_objects import BookingStatus, PaymentStatus
from app.booking.infrastructure.models import Booking
from app.booking.infrastructure.repository import BookingRepository
from app.common.pagination import make_paginated_response
from app.core.exceptions import BookingNotFoundError, PaymentProviderError
from app.core.logging import get_logger
from app.core.security import require_app_token
from app.payment.domain.gateway_protocol import CreatePaymentRequest
from app.payment.domain.parser import parse_gateway_response
from app.payment.domain.state_machine import PaymentStateMachine
from app.payment.domain.value_objects import (
    PaymentFor,
    PaymentGateway,
    PaymentMethod,
    PaymentProvider,
    PaymentThrough,
    PaymentTransactionStatus,
)
from app.payment.infrastructure.models import Payment, PaymentStatusHistory
from app.payment.infrastructure.providers.myfatoorah_provider import MyFatoorahProvider
from app.payment.infrastructure.providers.tap_provider import TapProvider
from app.payment.interfaces.schemas import (
    CreatePaymentRequestSchema,
    InitiatePaymentRequest,
    PaymentDetailResponse,
    PaymentListItem,
    PaymentListResponse,
    PaymentResponse,
    PaymentSessionResponse,
    PaymentStatusHistoryItem,
    PaymentStatusResponse,
    UpdatePaymentRequestSchema,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_provider(
    provider: str,
    http_client: httpx.AsyncClient,
    settings: object,
) -> object:
    """Return the correct payment provider implementation."""
    prov = provider.strip().lower() if provider else ""
    if prov in ("myfatoorah", "fatoorah"):
        return MyFatoorahProvider(http_client=http_client, settings=settings)  # type: ignore
    elif prov == "tap":
        return TapProvider(http_client=http_client, settings=settings)  # type: ignore
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported payment provider: {provider}",
    )


def _build_booking_snapshot(booking: Booking) -> dict[str, Any]:
    """Extract a rich audit snapshot from a Booking entity."""
    return {
        "booking_id": str(booking.id),
        "service_id": str(booking.service_id) if booking.service_id else None,
        "service_name": (booking.service_data or {}).get("name", ""),
        "service_category": (booking.service_data or {}).get("category", ""),
        "branch_id": str(booking.branch_id) if booking.branch_id else None,
        "branch_name": (booking.branch_data or {}).get("name", ""),
        "branch_address": (booking.branch_data or {}).get("address", ""),
        "service_arrangement_id": str(booking.service_arrangement_id) if booking.service_arrangement_id else None,
        "arrangement_type": (booking.service_arrangement_data or {}).get("arrangement_type", ""),
        "room_name": (booking.service_arrangement_data or {}).get("room_name", ""),
        "therapist_id": str(booking.therapist_id) if getattr(booking, "therapist_id", None) else None,
        "therapist_name": (
            (booking.therapist_data or {}).get("name")
            or (booking.therapist_data or {}).get("full_name")
            or ""
        ) if getattr(booking, "therapist_data", None) else "",
        "appointment_start": booking.appointment_start.isoformat() if booking.appointment_start else None,
        "appointment_end": booking.appointment_end.isoformat() if booking.appointment_end else None,
        "duration_minutes": getattr(booking, "duration_minutes", None),
        "extra_minutes": getattr(booking, "extra_minutes", None),
        "arrangement_price": str(getattr(booking, "arrangement_price", "0")),
        "price_for_extra_minutes": str(getattr(booking, "price_for_extra_minutes", "0")),
        "addon_price": str(getattr(booking, "addon_price", "0")),
        "discount": str(getattr(booking, "discount", "0")),
        "tax": str(getattr(booking, "tax", "0")),
        "fees": str(getattr(booking, "fees", "0")),
        "total_amount": str(getattr(booking, "total_amount", "0")),
        "currency": getattr(booking, "currency", "KWD"),
        "addons": getattr(booking, "addons", []) or [],
        "customer_notes": getattr(booking, "customer_notes", None),
    }


def _resolve_created_by(current_user: Any, body: Any) -> uuid.UUID | None:
    """Resolve the API requester UUID for created_by."""
    # 1. Explicit override in body
    if getattr(body, "created_by", None):
        try:
            return uuid.UUID(str(body.created_by))
        except (ValueError, AttributeError):
            pass
    # 2. JWT sub claim
    if current_user and getattr(current_user, "sub", None):
        try:
            return uuid.UUID(str(current_user.sub))
        except (ValueError, AttributeError):
            pass
    return None


def _payment_to_detail(p: Payment) -> PaymentDetailResponse:
    """Map ORM Payment model to PaymentDetailResponse."""
    history = None
    raw_history = p.__dict__.get("status_history")
    if raw_history:
        history = [
            PaymentStatusHistoryItem(
                id=str(h.id),
                old_status=h.old_status,
                new_status=h.new_status,
                source=h.source,
                reason=h.reason,
                provider_reference=h.provider_reference,
                correlation_id=h.correlation_id,
                metadata=h.metadata_,
                created_at=h.created_at,
            )
            for h in sorted(raw_history, key=lambda x: x.created_at)
        ]

    return PaymentDetailResponse(
        id=str(p.id),
        customer_id=str(p.customer_id),
        customer_data=p.customer_data,
        sender_id=str(p.sender_id) if p.sender_id else None,
        sender_data=p.sender_data,
        service_id=str(p.service_id) if p.service_id else None,
        service_data=p.service_data,
        branch_id=str(p.branch_id) if p.branch_id else None,
        branch_data=p.branch_data,
        service_arrangement_id=str(p.service_arrangement_id) if p.service_arrangement_id else None,
        service_arrangement_data=p.service_arrangement_data,
        addons=p.addons,
        addons_price=str(p.addons_price) if p.addons_price is not None else None,
        extra_time=p.extra_time,
        price_for_extra_time=str(p.price_for_extra_time) if p.price_for_extra_time is not None else None,
        total_amount=str(p.total_amount),
        total_duration=p.total_duration,
        currency=p.currency,
        country=p.country,
        status=p.status,
        paid_at=p.paid_at,
        recipient_id=str(p.recipient_id) if p.recipient_id else None,
        recipient_phone=p.recipient_phone,
        recipient_data=p.recipient_data,
        booking_id=str(p.booking_id) if p.booking_id else None,
        booking_data=p.booking_data,
        voucher_id=str(p.voucher_id) if p.voucher_id else None,
        voucher_data=p.voucher_data,
        product_order_id=str(p.product_order_id) if p.product_order_id else None,
        product_order_items=p.product_order_items,
        invoice_id=p.invoice_id,
        invoice_value=str(p.invoice_value) if p.invoice_value is not None else None,
        payment_url=p.payment_url,
        transaction_id=p.transaction_id,
        track_id=p.track_id,
        reference_id=p.reference_id,
        transaction_status=p.transaction_status,
        transaction_date=p.transaction_date,
        receipt_image=p.receipt_image,
        payment_method=p.payment_method,
        payment_through=p.payment_through,
        payment_provider=p.payment_provider,
        payment_gateway=p.payment_gateway,
        payment_for=p.payment_for,
        payment_id=p.payment_id,
        payment_data=p.payment_data,
        created_by=str(p.created_by) if p.created_by else None,
        created_at=p.created_at,
        status_history=history,
    )


def _payment_to_list_item(p: Payment) -> PaymentListItem:
    """Map ORM Payment model to PaymentListItem."""
    return PaymentListItem(
        id=str(p.id),
        customer_id=str(p.customer_id),
        customer_data=p.customer_data,
        sender_id=str(p.sender_id) if p.sender_id else None,
        service_id=str(p.service_id) if p.service_id else None,
        branch_id=str(p.branch_id) if p.branch_id else None,
        service_arrangement_id=str(p.service_arrangement_id) if p.service_arrangement_id else None,
        addons_price=str(p.addons_price) if p.addons_price is not None else None,
        extra_time=p.extra_time,
        total_amount=str(p.total_amount),
        total_duration=p.total_duration,
        currency=p.currency,
        country=p.country,
        status=p.status,
        paid_at=p.paid_at,
        recipient_id=str(p.recipient_id) if p.recipient_id else None,
        recipient_phone=p.recipient_phone,
        booking_id=str(p.booking_id) if p.booking_id else None,
        voucher_id=str(p.voucher_id) if p.voucher_id else None,
        product_order_id=str(p.product_order_id) if p.product_order_id else None,
        invoice_id=p.invoice_id,
        invoice_value=str(p.invoice_value) if p.invoice_value is not None else None,
        payment_url=p.payment_url,
        transaction_id=p.transaction_id,
        track_id=p.track_id,
        reference_id=p.reference_id,
        transaction_status=p.transaction_status,
        transaction_date=p.transaction_date,
        payment_method=p.payment_method,
        payment_through=p.payment_through,
        payment_provider=p.payment_provider,
        payment_gateway=p.payment_gateway,
        payment_for=p.payment_for,
        payment_id=p.payment_id,
        created_by=str(p.created_by) if p.created_by else None,
        created_at=p.created_at,
    )


# ── CRUD Endpoints ────────────────────────────────────────────────────────────


@router.post(
    "/",
    summary="Create or ingest a payment record",
    description=(
        "Create a payment record directly or by ingesting a full gateway response. "
        "Required fields: customer_id, total_amount, total_duration, currency. "
        "created_by is automatically set from the API requester's JWT token. "
        "If gateway_response is provided it is parsed and merged into payment_data."
    ),
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentResponse,
)
async def create_payment(
    body: CreatePaymentRequestSchema,
    _: RequireAppToken,
    session: DBSession,
    settings: AppSettings,
) -> JSONResponse:
    """Create a new payment record or ingest gateway response."""
    parsed_gateway: dict[str, Any] = {}
    if body.gateway_response:
        parsed_gateway = parse_gateway_response(body.gateway_response)

    # ── 1. Resolve booking ────────────────────────────────────────────
    booking_id_raw = body.booking_id or parsed_gateway.get("customer_reference") or None
    booking = None
    booking_id: uuid.UUID | None = None
    if booking_id_raw:
        try:
            booking_id = uuid.UUID(str(booking_id_raw))
            stmt = select(Booking).where(Booking.id == booking_id)
            result = await session.execute(stmt)
            booking = result.scalar_one_or_none()
        except (ValueError, TypeError):
            pass

    # ── 2. Resolve voucher_id ─────────────────────────────────────────
    voucher_id: uuid.UUID | None = None
    if body.voucher_id:
        try:
            voucher_id = uuid.UUID(str(body.voucher_id))
        except (ValueError, TypeError):
            pass

    # ── 3. Resolve payment_for ────────────────────────────────────────
    payment_for_raw = body.payment_for or None
    if payment_for_raw:
        payment_for = PaymentFor.normalise(payment_for_raw).value
    elif voucher_id:
        payment_for = PaymentFor.GIFT_VOUCHER.value
    elif booking and getattr(booking, "booking_type", None) == "home":
        payment_for = PaymentFor.HOME_SERVICE.value
    elif booking:
        payment_for = PaymentFor.BRANCH_SERVICE.value
    else:
        payment_for = PaymentFor.BRANCH_SERVICE.value

    # ── 4. Normalise classification fields ────────────────────────────
    payment_provider_val = body.payment_provider or parsed_gateway.get("payment_provider") or None
    if payment_provider_val:
        payment_provider = PaymentProvider.normalise(payment_provider_val).value
    else:
        payment_provider = None

    payment_through_val = body.payment_through or None
    if payment_through_val:
        payment_through = PaymentThrough.normalise(payment_through_val).value
    else:
        payment_through = None

    payment_gateway_val = body.payment_gateway or parsed_gateway.get("payment_gateway") or None
    if payment_gateway_val:
        payment_gateway = PaymentGateway.normalise(payment_gateway_val).value
    else:
        payment_gateway = None

    # ── 5. Customer data ──────────────────────────────────────────────
    customer_data = dict(body.customer_data or {})
    if not customer_data and parsed_gateway.get("customer_data"):
        customer_data = parsed_gateway["customer_data"]
    if booking and getattr(booking, "customer_data", None):
        customer_data = {**booking.customer_data, **customer_data}

    # ── 6. Booking data ───────────────────────────────────────────────
    booking_data = dict(body.booking_data or {})
    if booking and not booking_data:
        booking_data = _build_booking_snapshot(booking)

    # ── 7. Financial fields ───────────────────────────────────────────
    total_amount_val = body.total_amount or parsed_gateway.get("total_amount") or (
        getattr(booking, "total_amount", None) if booking else None
    ) or Decimal("0.000")
    total_amount = Decimal(str(total_amount_val))

    total_duration_val = body.total_duration or (
        getattr(booking, "duration_minutes", None) if booking else None
    ) or 0
    total_duration = int(total_duration_val)

    currency = body.currency or parsed_gateway.get("currency") or (
        getattr(booking, "currency", "KWD") if booking else "KWD"
    )
    if currency in ("KD", "KWD"):
        currency = "KWD"

    # ── 8. Invoice & transaction identifiers ──────────────────────────
    invoice_id = body.invoice_id or parsed_gateway.get("invoice_id") or None
    invoice_value_raw = body.invoice_value or parsed_gateway.get("invoice_value") or total_amount
    invoice_value = Decimal(str(invoice_value_raw)) if invoice_value_raw is not None else total_amount
    payment_url = body.payment_url or parsed_gateway.get("payment_url") or None
    transaction_id = body.transaction_id or parsed_gateway.get("transaction_id") or None
    track_id = body.track_id or parsed_gateway.get("track_id") or None
    reference_id = body.reference_id or parsed_gateway.get("reference_id") or None
    transaction_status = body.transaction_status or parsed_gateway.get("transaction_status") or None
    transaction_date = body.transaction_date or parsed_gateway.get("transaction_date") or None

    # ── 9. Status & paid_at ───────────────────────────────────────────
    status_str = body.status or parsed_gateway.get("status") or PaymentTransactionStatus.INITIATED.value
    paid_at = body.paid_at or parsed_gateway.get("paid_at") or (
        datetime.now(tz=timezone.utc) if status_str == PaymentTransactionStatus.SUCCESS.value else None
    )

    # ── 10. Merge payment_data ────────────────────────────────────────
    # Start with parsed gateway data block, then overlay explicit body.payment_data
    payment_data: dict[str, Any] = {}
    if parsed_gateway.get("payment_data"):
        payment_data.update(parsed_gateway["payment_data"])
    if body.payment_data:
        payment_data.update(body.payment_data)

    # ── 11. Service & arrangement IDs from booking ────────────────────
    service_id = body.service_id or (
        uuid.UUID(str(booking.service_id)) if booking and getattr(booking, "service_id", None) else None
    )
    branch_id = body.branch_id or (
        uuid.UUID(str(booking.branch_id)) if booking and getattr(booking, "branch_id", None) else None
    )
    service_arrangement_id = body.service_arrangement_id or (
        uuid.UUID(str(booking.service_arrangement_id))
        if booking and getattr(booking, "service_arrangement_id", None) else None
    )
    service_data = body.service_data or (booking.service_data if booking else None) or None
    branch_data = body.branch_data or (booking.branch_data if booking else None) or None
    service_arrangement_data = body.service_arrangement_data or (
        booking.service_arrangement_data if booking else None
    ) or None

    # Addons from booking if not in body
    addons = body.addons
    addons_price = body.addons_price
    extra_time = body.extra_time
    price_for_extra_time = body.price_for_extra_time
    if booking and addons is None:
        addons = getattr(booking, "addons", None)
    if booking and addons_price is None:
        raw_addon_price = getattr(booking, "addon_price", None)
        if raw_addon_price is not None:
            addons_price = Decimal(str(raw_addon_price))
    if booking and extra_time is None:
        extra_time = getattr(booking, "extra_minutes", None)
    if booking and price_for_extra_time is None:
        raw_extra_price = getattr(booking, "price_for_extra_minutes", None)
        if raw_extra_price is not None:
            price_for_extra_time = Decimal(str(raw_extra_price))

    # ── 12. Create Payment record ─────────────────────────────────────
    payment = Payment(
        customer_id=body.customer_id,
        customer_data=customer_data or None,
        sender_id=body.sender_id,
        sender_data=body.sender_data,
        service_id=service_id,
        service_data=service_data,
        branch_id=branch_id,
        branch_data=branch_data,
        service_arrangement_id=service_arrangement_id,
        service_arrangement_data=service_arrangement_data,
        addons=addons,
        addons_price=addons_price,
        extra_time=extra_time,
        price_for_extra_time=price_for_extra_time,
        total_amount=total_amount,
        total_duration=total_duration,
        currency=currency,
        country=body.country or parsed_gateway.get("country"),
        status=status_str,
        paid_at=paid_at,
        recipient_id=body.recipient_id,
        recipient_phone=body.recipient_phone,
        recipient_data=body.recipient_data,
        booking_id=booking_id,
        booking_data=booking_data or None,
        voucher_id=voucher_id,
        voucher_data=dict(body.voucher_data or {}) or None,
        product_order_id=body.product_order_id,
        product_order_items=body.product_order_items,
        invoice_id=invoice_id,
        invoice_value=invoice_value,
        payment_url=payment_url,
        transaction_id=transaction_id,
        track_id=track_id,
        reference_id=reference_id,
        transaction_status=transaction_status,
        transaction_date=transaction_date,
        receipt_image=body.receipt_image,
        payment_method=body.payment_method or parsed_gateway.get("payment_method"),
        payment_through=payment_through,
        payment_provider=payment_provider,
        payment_gateway=payment_gateway,
        payment_for=payment_for,
        payment_id=body.payment_id or parsed_gateway.get("payment_id"),
        payment_data=payment_data or None,
        # Use explicit created_by from body (e.g. ushnotice passes voucher creator UUID).
        # This endpoint uses RequireAppToken (no CurrentUser JWT), so we cannot auto-derive it.
        created_by=body.created_by if body.created_by else None,
    )

    session.add(payment)
    await session.flush()
    await session.refresh(payment)

    # ── 13. Status history ────────────────────────────────────────────
    history = PaymentStatusHistory(
        payment_id=payment.id,
        old_status=None,
        new_status=payment.status,
        source="gateway_webhook" if body.gateway_response else "manual",
        reason="Payment record created",
        provider_reference=(payment.payment_data or {}).get("provider_reference"),
        correlation_id=payment.payment_id or str(payment.id),
        metadata_=None,
    )
    session.add(history)
    await session.flush()

    # ── 14. Update booking payments_meta & auto-confirm if SUCCESS ────
    if booking:
        payment_meta_snapshot = {
            "payment_id": payment.payment_id or str(payment.id),
            "transaction_id": payment.transaction_id,
            "invoice_id": payment.invoice_id,
            "invoice_value": str(payment.invoice_value or payment.total_amount),
            "status": payment.status,
            "reference_id": payment.reference_id,
            "transaction_date": payment.transaction_date,
            "payment_gateway": payment.payment_gateway,
            "payment_provider": payment.payment_provider,
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        }
        booking.payments_meta = {**(booking.payments_meta or {}), **payment_meta_snapshot}
        if payment.status == PaymentTransactionStatus.SUCCESS.value and payment.booking_id:
            try:
                booking_service = BookingService(session=session, settings=settings)
                await booking_service.confirm_booking(
                    payment.booking_id,
                    payment_id=str(payment.id),
                    payments_meta=payment_meta_snapshot,
                    correlation_id=payment.payment_id or str(payment.id),
                )
            except Exception as exc:
                logger.warning("payment_auto_confirm_booking_notice", error=str(exc))

    logger.info("payment_record_created", payment_id=str(payment.id), status=payment.status)

    response_data = _payment_to_detail(payment)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"success": True, "data": response_data.model_dump(mode="json")},
    )


@router.get(
    "/",
    summary="List payments",
    description="List and filter payments with pagination and aggregate summary metrics for dashboards.",
    response_model=PaymentListResponse,
)
async def list_payments(
    current_user: CurrentUser,
    session: DBSession,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    booking_id: uuid.UUID | None = Query(default=None),
    voucher_id: uuid.UUID | None = Query(default=None),
    product_order_id: uuid.UUID | None = Query(default=None),
    payment_for_filter: str | None = Query(default=None, alias="payment_for"),
    customer_id: uuid.UUID | None = Query(default=None),
    sender_id: uuid.UUID | None = Query(default=None),
    service_id: uuid.UUID | None = Query(default=None),
    branch_id: uuid.UUID | None = Query(default=None),
    recipient_id: uuid.UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    payment_provider_filter: str | None = Query(default=None, alias="payment_provider"),
    payment_through_filter: str | None = Query(default=None, alias="payment_through"),
    payment_gateway_filter: str | None = Query(default=None, alias="payment_gateway"),
    payment_id_filter: str | None = Query(default=None, alias="payment_id"),
    transaction_id_filter: str | None = Query(default=None, alias="transaction_id"),
    invoice_id_filter: str | None = Query(default=None, alias="invoice_id"),
    created_by_filter: uuid.UUID | None = Query(default=None, alias="created_by"),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    search: str | None = Query(
        default=None,
        description="Search by payment_id, transaction_id, invoice_id, reference_id, track_id, recipient_phone.",
    ),
) -> JSONResponse:
    """List payments with filtering and financial analytics summary."""
    conditions = []

    if booking_id:
        conditions.append(Payment.booking_id == booking_id)
    if voucher_id:
        conditions.append(Payment.voucher_id == voucher_id)
    if product_order_id:
        conditions.append(Payment.product_order_id == product_order_id)
    if payment_for_filter:
        conditions.append(Payment.payment_for == PaymentFor.normalise(payment_for_filter).value)
    if customer_id:
        conditions.append(Payment.customer_id == customer_id)
    if sender_id:
        conditions.append(Payment.sender_id == sender_id)
    if service_id:
        conditions.append(Payment.service_id == service_id)
    if branch_id:
        conditions.append(Payment.branch_id == branch_id)
    if recipient_id:
        conditions.append(Payment.recipient_id == recipient_id)
    if status_filter:
        conditions.append(Payment.status == status_filter)
    if payment_provider_filter:
        conditions.append(Payment.payment_provider == PaymentProvider.normalise(payment_provider_filter).value)
    if payment_through_filter:
        conditions.append(Payment.payment_through == PaymentThrough.normalise(payment_through_filter).value)
    if payment_gateway_filter:
        conditions.append(Payment.payment_gateway == PaymentGateway.normalise(payment_gateway_filter).value)
    if payment_id_filter:
        conditions.append(Payment.payment_id == payment_id_filter)
    if transaction_id_filter:
        conditions.append(Payment.transaction_id == transaction_id_filter)
    if invoice_id_filter:
        conditions.append(Payment.invoice_id == invoice_id_filter)
    if created_by_filter:
        conditions.append(Payment.created_by == created_by_filter)
    if from_date:
        conditions.append(Payment.created_at >= from_date)
    if to_date:
        conditions.append(Payment.created_at <= to_date)
    if search:
        search_pattern = f"%{search.strip()}%"
        conditions.append(
            or_(
                Payment.payment_id.ilike(search_pattern),
                Payment.transaction_id.ilike(search_pattern),
                Payment.invoice_id.ilike(search_pattern),
                Payment.reference_id.ilike(search_pattern),
                Payment.track_id.ilike(search_pattern),
                Payment.recipient_phone.ilike(search_pattern),
            )
        )

    # Count query
    count_stmt = select(func.count(Payment.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total_count = (await session.execute(count_stmt)).scalar_one()

    # Aggregate financial metrics
    sum_stmt = select(
        func.coalesce(func.sum(Payment.total_amount), Decimal("0.000")).label("total_volume"),
    )
    if conditions:
        sum_stmt = sum_stmt.where(*conditions)
    agg_res = (await session.execute(sum_stmt)).one()

    # Data query
    offset = (page - 1) * page_size
    data_stmt = (
        select(Payment)
        .order_by(Payment.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    if conditions:
        data_stmt = data_stmt.where(*conditions)

    result = await session.execute(data_stmt)
    payments = result.scalars().all()

    items = [_payment_to_list_item(p) for p in payments]

    paginated = make_paginated_response(
        items,
        count=total_count,
        page=page,
        page_size=page_size,
        base_url=str(request.url.remove_query_params(["page", "page_size"])),
    )

    out = paginated.model_dump(mode="json")
    out["analytics"] = {
        "total_volume": str(agg_res.total_volume),
    }

    return JSONResponse(content=out)


@router.get(
    "/{payment_id}/",
    summary="Get payment detail",
    description="Retrieve full payment record with transaction audit trail and all snapshots.",
    response_model=PaymentResponse,
)
async def get_payment(
    payment_id: uuid.UUID,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Get payment detail by ID."""
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found.",
        )

    response_data = _payment_to_detail(payment)
    return JSONResponse(
        content={"success": True, "data": response_data.model_dump(mode="json")}
    )


@router.patch(
    "/{payment_id}/",
    summary="Update payment (partial)",
    description="Partially update payment details, status, or classification fields.",
    response_model=PaymentResponse,
)
@router.put(
    "/{payment_id}/",
    summary="Update payment (full)",
    description="Update payment record.",
    response_model=PaymentResponse,
)
async def update_payment(
    payment_id: uuid.UUID,
    body: UpdatePaymentRequestSchema,
    current_user: CurrentUser,
    session: DBSession,
    settings: AppSettings,
) -> JSONResponse:
    """Update payment record."""
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found.",
        )

    old_status = payment.status

    # Process gateway response if supplied
    if body.gateway_response:
        parsed = parse_gateway_response(body.gateway_response)
        # Merge raw data into payment_data
        existing_payment_data = dict(payment.payment_data or {})
        if parsed.get("payment_data"):
            existing_payment_data.update(parsed["payment_data"])
        existing_payment_data["raw_response"] = body.gateway_response
        payment.payment_data = existing_payment_data

        for field in (
            "status", "total_amount", "currency", "invoice_id", "invoice_value",
            "transaction_id", "payment_id", "track_id", "reference_id",
            "transaction_status", "transaction_date", "payment_method",
            "payment_gateway", "payment_url", "country", "paid_at",
        ):
            val = parsed.get(field)
            if val is not None:
                setattr(payment, field, val)
        if parsed.get("customer_data"):
            payment.customer_data = {**(payment.customer_data or {}), **parsed["customer_data"]}

    # Apply explicit body fields
    if body.customer_data is not None:
        payment.customer_data = {**(payment.customer_data or {}), **body.customer_data}
    if body.sender_id is not None:
        payment.sender_id = body.sender_id
    if body.sender_data is not None:
        payment.sender_data = {**(payment.sender_data or {}), **body.sender_data}
    if body.service_id is not None:
        payment.service_id = body.service_id
    if body.service_data is not None:
        payment.service_data = {**(payment.service_data or {}), **body.service_data}
    if body.branch_id is not None:
        payment.branch_id = body.branch_id
    if body.branch_data is not None:
        payment.branch_data = {**(payment.branch_data or {}), **body.branch_data}
    if body.service_arrangement_id is not None:
        payment.service_arrangement_id = body.service_arrangement_id
    if body.service_arrangement_data is not None:
        payment.service_arrangement_data = {**(payment.service_arrangement_data or {}), **body.service_arrangement_data}
    if body.addons is not None:
        payment.addons = body.addons
    if body.addons_price is not None:
        payment.addons_price = Decimal(str(body.addons_price))
    if body.extra_time is not None:
        payment.extra_time = body.extra_time
    if body.price_for_extra_time is not None:
        payment.price_for_extra_time = Decimal(str(body.price_for_extra_time))
    if body.total_amount is not None:
        payment.total_amount = Decimal(str(body.total_amount))
    if body.total_duration is not None:
        payment.total_duration = body.total_duration
    if body.currency is not None:
        payment.currency = body.currency
    if body.country is not None:
        payment.country = body.country
    if body.status is not None:
        payment.status = body.status
    if body.paid_at is not None:
        payment.paid_at = body.paid_at
    elif payment.status == PaymentTransactionStatus.SUCCESS.value and not payment.paid_at:
        payment.paid_at = datetime.now(tz=timezone.utc)
    if body.recipient_id is not None:
        payment.recipient_id = body.recipient_id
    if body.recipient_phone is not None:
        payment.recipient_phone = body.recipient_phone
    if body.recipient_data is not None:
        payment.recipient_data = {**(payment.recipient_data or {}), **body.recipient_data}
    if body.booking_id is not None:
        payment.booking_id = body.booking_id
    if body.booking_data is not None:
        payment.booking_data = {**(payment.booking_data or {}), **body.booking_data}
    if body.voucher_id is not None:
        payment.voucher_id = body.voucher_id
    if body.voucher_data is not None:
        payment.voucher_data = {**(payment.voucher_data or {}), **body.voucher_data}
    if body.product_order_id is not None:
        payment.product_order_id = body.product_order_id
    if body.product_order_items is not None:
        payment.product_order_items = body.product_order_items
    if body.invoice_id is not None:
        payment.invoice_id = body.invoice_id
    if body.invoice_value is not None:
        payment.invoice_value = Decimal(str(body.invoice_value))
    if body.payment_url is not None:
        payment.payment_url = body.payment_url
    if body.transaction_id is not None:
        payment.transaction_id = body.transaction_id
    if body.track_id is not None:
        payment.track_id = body.track_id
    if body.reference_id is not None:
        payment.reference_id = body.reference_id
    if body.transaction_status is not None:
        payment.transaction_status = body.transaction_status
    if body.transaction_date is not None:
        payment.transaction_date = body.transaction_date
    if body.receipt_image is not None:
        payment.receipt_image = body.receipt_image
    if body.payment_method is not None:
        payment.payment_method = body.payment_method
    if body.payment_through is not None:
        payment.payment_through = PaymentThrough.normalise(body.payment_through).value
    if body.payment_provider is not None:
        payment.payment_provider = PaymentProvider.normalise(body.payment_provider).value
    if body.payment_gateway is not None:
        payment.payment_gateway = PaymentGateway.normalise(body.payment_gateway).value
    if body.payment_for is not None:
        payment.payment_for = PaymentFor.normalise(body.payment_for).value
    if body.payment_id is not None:
        payment.payment_id = body.payment_id
    if body.payment_data is not None:
        payment.payment_data = {**(payment.payment_data or {}), **body.payment_data}

    # Record status change in audit trail
    if payment.status != old_status:
        history = PaymentStatusHistory(
            payment_id=payment.id,
            old_status=old_status,
            new_status=payment.status,
            source=body.source,
            reason=body.reason or "Payment updated",
            provider_reference=(payment.payment_data or {}).get("provider_reference"),
            correlation_id=payment.payment_id or str(payment.id),
            metadata_=None,
        )
        session.add(history)

        # Auto-confirm booking on success transition
        if payment.status == PaymentTransactionStatus.SUCCESS.value and payment.booking_id:
            payment_meta_snapshot = {
                "payment_id": payment.payment_id or str(payment.id),
                "transaction_id": payment.transaction_id,
                "invoice_id": payment.invoice_id,
                "invoice_value": str(payment.invoice_value or payment.total_amount),
                "status": payment.status,
                "reference_id": payment.reference_id,
                "transaction_date": payment.transaction_date,
                "payment_gateway": payment.payment_gateway,
                "payment_provider": payment.payment_provider,
                "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            }
            try:
                booking_service = BookingService(session=session, settings=settings)
                await booking_service.confirm_booking(
                    payment.booking_id,
                    payment_id=str(payment.id),
                    payments_meta=payment_meta_snapshot,
                    correlation_id=payment.payment_id or str(payment.id),
                )
            except Exception as exc:
                logger.warning("payment_update_auto_confirm_notice", error=str(exc))

    await session.flush()
    await session.refresh(payment)

    response_data = _payment_to_detail(payment)
    return JSONResponse(
        content={"success": True, "data": response_data.model_dump(mode="json")}
    )


@router.delete(
    "/{payment_id}/",
    summary="Delete payment",
    description="Delete a payment record.",
)
async def delete_payment(
    payment_id: uuid.UUID,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Delete a payment record."""
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found.",
        )

    await session.delete(payment)
    await session.flush()

    return JSONResponse(
        content={
            "success": True,
            "message": f"Payment record {payment_id} deleted successfully.",
        }
    )


# ── Gateway Integration & Webhook Endpoints ───────────────────────────────────


@router.post(
    "/initiate/",
    summary="Initiate payment for a booking",
    status_code=status.HTTP_201_CREATED,
)
async def initiate_payment(
    body: InitiatePaymentRequest,
    current_user: CurrentUser,
    session: DBSession,
    settings: AppSettings,
) -> JSONResponse:
    """
    Initiate a payment session for a confirmed-pending booking.

    Returns a payment_url that the mobile app should open in a browser/webview.
    """
    from app.api.deps import get_http_client

    booking_id = uuid.UUID(body.booking_id)
    customer_id = uuid.UUID(current_user.sub)

    stmt = select(Booking).where(Booking.id == booking_id, Booking.customer_id == customer_id)
    result = await session.execute(stmt)
    booking = result.scalar_one_or_none()

    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking {booking_id} not found or does not belong to you.",
        )

    if booking.status not in (BookingStatus.CONFIRMED.value, BookingStatus.PENDING.value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Booking must be confirmed or pending to initiate payment. Current: {booking.status}",
        )

    provider_key = body.provider.strip().lower() if body.provider else "myfatoorah"

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        provider_impl = _get_provider(provider_key, http_client, settings)

        total_amount = Decimal(str(getattr(booking, "total_amount", 0)))
        currency = getattr(booking, "currency", "KWD")
        if currency in ("KD",):
            currency = "KWD"

        pay_request = CreatePaymentRequest(
            amount=total_amount,
            currency=currency,
            customer_id=str(customer_id),
            booking_id=str(booking_id),
            customer_name=(booking.customer_data or {}).get("name", ""),
            customer_mobile=(booking.customer_data or {}).get("mobile", ""),
            customer_email=(booking.customer_data or {}).get("email", ""),
            callback_url=str(getattr(settings, "PAYMENT_CALLBACK_URL", "")),
            error_url=str(getattr(settings, "PAYMENT_ERROR_URL", "")),
        )

        try:
            session_response = await provider_impl.initiate_payment(pay_request)
        except Exception as exc:
            logger.error("payment_initiate_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Payment provider error: {exc}",
            )

    payment_url = session_response.get("payment_url") or session_response.get("PaymentURL", "")
    gateway_payment_id = session_response.get("payment_id") or session_response.get("InvoiceId", "")

    # Create a pending payment record
    payment = Payment(
        customer_id=customer_id,
        booking_id=booking_id,
        booking_data=_build_booking_snapshot(booking),
        customer_data=booking.customer_data,
        total_amount=total_amount,
        total_duration=getattr(booking, "duration_minutes", 0) or 0,
        currency=currency,
        payment_for=PaymentFor.normalise(body.payment_for).value,
        payment_provider=PaymentProvider.normalise(body.provider).value if body.provider else PaymentProvider.MYFATOORAH.value,
        payment_method=body.payment_method or "card",
        status=PaymentTransactionStatus.INITIATED.value,
        payment_id=str(gateway_payment_id) if gateway_payment_id else None,
        payment_url=payment_url,
        payment_data={"session_response": session_response},
        created_by=customer_id,
    )
    session.add(payment)
    await session.flush()

    history = PaymentStatusHistory(
        payment_id=payment.id,
        old_status=None,
        new_status=PaymentTransactionStatus.INITIATED.value,
        source="initiate_payment",
        reason="Payment session initiated",
        correlation_id=str(gateway_payment_id) if gateway_payment_id else str(payment.id),
    )
    session.add(history)
    await session.flush()

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "success": True,
            "data": {
                "payment_id": str(payment.id),
                "booking_id": str(booking_id),
                "payment_for": payment.payment_for,
                "payment_provider": payment.payment_provider,
                "payment_url": payment_url,
                "total_amount": str(total_amount),
                "currency": currency,
            },
        },
    )


@router.get(
    "/{booking_id}/status/",
    summary="Get payment status for a booking",
    response_model=PaymentStatusResponse,
)
async def get_payment_status(
    booking_id: uuid.UUID,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Get the most recent payment status for a booking."""
    stmt = (
        select(Payment)
        .where(Payment.booking_id == booking_id)
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No payment found for booking {booking_id}.",
        )

    return JSONResponse(
        content={
            "success": True,
            "data": {
                "payment_id": str(payment.id),
                "booking_id": str(payment.booking_id) if payment.booking_id else None,
                "voucher_id": str(payment.voucher_id) if payment.voucher_id else None,
                "payment_for": payment.payment_for,
                "payment_provider": payment.payment_provider,
                "status": payment.status,
                "total_amount": str(payment.total_amount),
                "currency": payment.currency,
                "payment_method": payment.payment_method,
                "reference_id": payment.reference_id,
                "created_at": payment.created_at.isoformat(),
            },
        }
    )


@router.post(
    "/webhook/myfatoorah/",
    summary="MyFatoorah payment webhook",
    include_in_schema=False,
)
async def myfatoorah_webhook(
    request: Request,
    _: RequireAppToken,
    session: DBSession,
    settings: AppSettings,
) -> JSONResponse:
    """Receive and process MyFatoorah webhook events."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    parsed = parse_gateway_response(payload)

    invoice_id = parsed.get("invoice_id") or (parsed.get("payment_data") or {}).get("provider_payment_id")
    if not invoice_id:
        return JSONResponse(content={"received": True})

    # Find payment by invoice_id or payment_id
    stmt = select(Payment).where(
        or_(Payment.invoice_id == str(invoice_id), Payment.payment_id == str(invoice_id))
    )
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        # Create new payment from webhook
        customer_reference = (parsed.get("payment_data") or {}).get("customer_reference")
        booking_id = None
        booking = None
        if customer_reference:
            try:
                booking_id = uuid.UUID(str(customer_reference))
                b_stmt = select(Booking).where(Booking.id == booking_id)
                b_result = await session.execute(b_stmt)
                booking = b_result.scalar_one_or_none()
            except (ValueError, TypeError):
                pass

        payment = Payment(
            customer_id=booking.customer_id if booking else uuid.uuid4(),
            customer_data=parsed.get("customer_data"),
            booking_id=booking_id,
            booking_data=_build_booking_snapshot(booking) if booking else None,
            total_amount=parsed.get("total_amount", Decimal("0.000")),
            total_duration=getattr(booking, "duration_minutes", 0) or 0,
            currency=parsed.get("currency", "KWD"),
            status=parsed.get("status", PaymentTransactionStatus.PENDING.value),
            paid_at=parsed.get("paid_at"),
            invoice_id=parsed.get("invoice_id"),
            invoice_value=parsed.get("invoice_value"),
            payment_id=parsed.get("payment_id"),
            transaction_id=parsed.get("transaction_id"),
            track_id=parsed.get("track_id"),
            reference_id=parsed.get("reference_id"),
            transaction_status=parsed.get("transaction_status"),
            transaction_date=parsed.get("transaction_date"),
            payment_method=parsed.get("payment_method"),
            payment_gateway=parsed.get("payment_gateway"),
            payment_url=parsed.get("payment_url"),
            country=parsed.get("country"),
            payment_provider=PaymentProvider.MYFATOORAH.value,
            payment_for=PaymentFor.BRANCH_SERVICE.value,
            payment_data=parsed.get("payment_data"),
        )
        session.add(payment)
        await session.flush()
    else:
        old_status = payment.status
        # Update existing payment from webhook
        existing_pdata = dict(payment.payment_data or {})
        if parsed.get("payment_data"):
            existing_pdata.update(parsed["payment_data"])
        payment.payment_data = existing_pdata

        for field in (
            "status", "total_amount", "currency", "invoice_id", "invoice_value",
            "transaction_id", "payment_id", "track_id", "reference_id",
            "transaction_status", "transaction_date", "payment_method",
            "payment_gateway", "payment_url", "country", "paid_at",
        ):
            val = parsed.get(field)
            if val is not None:
                setattr(payment, field, val)
        if parsed.get("customer_data"):
            payment.customer_data = {**(payment.customer_data or {}), **parsed["customer_data"]}

        if payment.status != old_status:
            history = PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status,
                new_status=payment.status,
                source="webhook_myfatoorah",
                reason="MyFatoorah webhook update",
                provider_reference=(payment.payment_data or {}).get("provider_reference"),
                correlation_id=payment.payment_id or str(payment.id),
            )
            session.add(history)

    # Auto-confirm booking if success
    if payment.status == PaymentTransactionStatus.SUCCESS.value and payment.booking_id:
        try:
            payment_meta_snapshot = {
                "payment_id": payment.payment_id or str(payment.id),
                "invoice_id": payment.invoice_id,
                "status": payment.status,
                "payment_gateway": payment.payment_gateway,
                "payment_provider": payment.payment_provider,
                "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            }
            booking_service = BookingService(session=session, settings=settings)
            await booking_service.confirm_booking(
                payment.booking_id,
                payment_id=str(payment.id),
                payments_meta=payment_meta_snapshot,
                correlation_id=payment.payment_id or str(payment.id),
            )
        except Exception as exc:
            logger.warning("webhook_auto_confirm_notice", error=str(exc))

    await session.flush()
    logger.info("myfatoorah_webhook_processed", payment_id=str(payment.id), status=payment.status)

    return JSONResponse(content={"received": True, "payment_id": str(payment.id)})


@router.post(
    "/webhook/tap/",
    summary="Tap payment webhook",
    include_in_schema=False,
)
async def tap_webhook(
    request: Request,
    _: RequireAppToken,
    session: DBSession,
) -> JSONResponse:
    """Receive and process Tap payment webhook events."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    parsed = parse_gateway_response(payload)
    charge_id = parsed.get("payment_id") or payload.get("id")

    if not charge_id:
        return JSONResponse(content={"received": True})

    stmt = select(Payment).where(Payment.payment_id == str(charge_id))
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if payment:
        old_status = payment.status
        existing_pdata = dict(payment.payment_data or {})
        if parsed.get("payment_data"):
            existing_pdata.update(parsed["payment_data"])
        payment.payment_data = existing_pdata

        for field in ("status", "transaction_id", "track_id", "reference_id", "paid_at"):
            val = parsed.get(field)
            if val is not None:
                setattr(payment, field, val)

        if payment.status != old_status:
            history = PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status,
                new_status=payment.status,
                source="webhook_tap",
                reason="Tap webhook update",
                correlation_id=str(charge_id),
            )
            session.add(history)

        await session.flush()
        logger.info("tap_webhook_processed", payment_id=str(payment.id), status=payment.status)

    return JSONResponse(content={"received": True})
