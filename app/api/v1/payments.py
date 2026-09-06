"""
app/api/v1/payments.py
───────────────────────
Payment API endpoints for creating, listing, retrieving, updating, and deleting payments.

Routes:
  POST   /api/v1/payments/                    → Create payment / Ingest gateway response
  GET    /api/v1/payments/                    → List payments (paginated, filters, finance summary)
  GET    /api/v1/payments/{payment_id}/       → Get payment detail
  PATCH  /api/v1/payments/{payment_id}/       → Update payment
  PUT    /api/v1/payments/{payment_id}/       → Update payment (full)
  DELETE /api/v1/payments/{payment_id}/       → Delete payment
  POST   /api/v1/payments/initiate/           → Initiate payment session
  GET    /api/v1/payments/{booking_id}/status/ → Get payment status for booking
  POST   /api/v1/payments/webhook/myfatoorah/ → MyFatoorah webhook
  POST   /api/v1/payments/webhook/tap/        → Tap webhook
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
from app.payment.domain.value_objects import PaymentFor, PaymentProvider, PaymentTransactionStatus
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


def _get_provider(
    provider: PaymentProvider,
    http_client: httpx.AsyncClient,
    settings: object,
) -> object:
    """Return the correct payment provider implementation."""
    if provider == PaymentProvider.MYFATOORAH:
        return MyFatoorahProvider(http_client=http_client, settings=settings)  # type: ignore
    elif provider == PaymentProvider.TAP:
        return TapProvider(http_client=http_client, settings=settings)  # type: ignore
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported payment provider: {provider}",
    )


def _build_booking_snapshot(booking: Booking) -> dict[str, Any]:
    """Extract a rich audit snapshot from a Booking entity."""
    return {
        "booking_id": str(booking.id),
        "service_id": str(booking.service_id),
        "service_name": (booking.service_data or {}).get("name", ""),
        "service_category": (booking.service_data or {}).get("category", ""),
        "branch_id": str(booking.branch_id),
        "branch_name": (booking.branch_data or {}).get("name", ""),
        "branch_address": (booking.branch_data or {}).get("address", ""),
        "service_arrangement_id": str(booking.service_arrangement_id),
        "arrangement_type": (booking.service_arrangement_data or {}).get("arrangement_type", ""),
        "room_name": (booking.service_arrangement_data or {}).get("room_name", ""),
        "therapist_id": str(booking.therapist_id),
        "therapist_name": (
            (booking.therapist_data or {}).get("name")
            or (booking.therapist_data or {}).get("full_name")
            or ""
        ),
        "appointment_start": booking.appointment_start.isoformat() if booking.appointment_start else None,
        "appointment_end": booking.appointment_end.isoformat() if booking.appointment_end else None,
        "duration_minutes": booking.duration_minutes,
        "extra_minutes": booking.extra_minutes,
        "arrangement_price": str(booking.arrangement_price),
        "price_for_extra_minutes": str(booking.price_for_extra_minutes),
        "addon_price": str(booking.addon_price),
        "discount": str(booking.discount),
        "tax": str(booking.tax),
        "fees": str(booking.fees),
        "total_amount": str(booking.total_amount),
        "currency": booking.currency,
        "addons": booking.addons or [],
        "customer_notes": booking.customer_notes,
    }


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
        booking_id=str(p.booking_id) if p.booking_id else None,
        customer_id=str(p.customer_id),
        voucher_id=str(p.voucher_id) if getattr(p, "voucher_id", None) else None,
        voucher_data=getattr(p, "voucher_data", None),
        payment_for=getattr(p, "payment_for", "service") or "service",
        amount=str(p.amount),
        currency=p.currency,
        service_charge=str(p.service_charge or Decimal("0.000")),
        vat_amount=str(p.vat_amount or Decimal("0.000")),
        due_deposit=str(p.due_deposit) if p.due_deposit is not None else None,
        deposit_status=p.deposit_status,
        provider=p.provider,
        gateway_name=p.gateway_name,
        payment_gateway=p.payment_gateway or p.gateway_name,
        payment_method=p.payment_method,
        status=p.status,
        is_paid=p.is_paid or (p.status == "success"),
        payment_id=p.payment_id or p.payment_id_gateway,
        transaction_id=p.transaction_id or p.provider_transaction_id,
        invoice_id=p.invoice_id or p.provider_payment_id,
        invoice_value=str(p.invoice_value) if p.invoice_value is not None else str(p.amount),
        invoice_reference=p.invoice_reference,
        customer_reference=p.customer_reference,
        customer_name=p.customer_name or (p.customer_data.get("name") if p.customer_data else None),
        customer_mobile=p.customer_mobile or (p.customer_data.get("mobile") or p.customer_data.get("phone_number") if p.customer_data else None),
        customer_email=p.customer_email or (p.customer_data.get("email") if p.customer_data else None),
        created_date=p.created_date or (p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else None),
        transaction_date=p.transaction_date or (p.paid_at.strftime("%Y-%m-%d %H:%M:%S") if p.paid_at else None),
        provider_payment_id=p.provider_payment_id,
        provider_reference=p.provider_reference,
        provider_transaction_id=p.provider_transaction_id,
        reference_id=p.reference_id,
        track_id=p.track_id,
        authorization_id=p.authorization_id,
        payment_id_gateway=p.payment_id_gateway,
        payment_url=p.payment_url,
        failure_reason=p.failure_reason,
        ip_address=p.ip_address,
        country=p.country,
        paid_at=p.paid_at,
        customer_data=p.customer_data,
        booking_data=p.booking_data,
        card_info=p.card_info,
        metadata=p.metadata_,
        provider_response=p.provider_response,
        status_history=history,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _payment_to_list_item(p: Payment) -> PaymentListItem:
    """Map ORM Payment model to PaymentListItem."""
    return PaymentListItem(
        id=str(p.id),
        booking_id=str(p.booking_id) if p.booking_id else None,
        customer_id=str(p.customer_id),
        voucher_id=str(p.voucher_id) if getattr(p, "voucher_id", None) else None,
        voucher_data=getattr(p, "voucher_data", None),
        payment_for=getattr(p, "payment_for", "service") or "service",
        amount=str(p.amount),
        currency=p.currency,
        provider=p.provider,
        payment_method=p.payment_method,
        status=p.status,
        is_paid=p.is_paid or (p.status == "success"),
        payment_id=p.payment_id or p.payment_id_gateway,
        transaction_id=p.transaction_id or p.provider_transaction_id,
        invoice_id=p.invoice_id or p.provider_payment_id,
        invoice_value=str(p.invoice_value) if p.invoice_value is not None else str(p.amount),
        invoice_reference=p.invoice_reference,
        customer_reference=p.customer_reference,
        customer_name=p.customer_name or (p.customer_data.get("name") if p.customer_data else None),
        customer_mobile=p.customer_mobile or (p.customer_data.get("mobile") or p.customer_data.get("phone_number") if p.customer_data else None),
        customer_email=p.customer_email or (p.customer_data.get("email") if p.customer_data else None),
        created_date=p.created_date or (p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else None),
        transaction_date=p.transaction_date or (p.paid_at.strftime("%Y-%m-%d %H:%M:%S") if p.paid_at else None),
        payment_gateway=p.payment_gateway or p.gateway_name,
        gateway_name=p.gateway_name,
        reference_id=p.reference_id,
        track_id=p.track_id,
        service_charge=str(p.service_charge) if p.service_charge is not None else None,
        vat_amount=str(p.vat_amount) if p.vat_amount is not None else None,
        due_deposit=str(p.due_deposit) if p.due_deposit is not None else None,
        deposit_status=p.deposit_status,
        payment_url=p.payment_url,
        customer_data=p.customer_data,
        booking_data=p.booking_data,
        paid_at=p.paid_at,
        created_at=p.created_at,
    )


# ── CRUD Endpoints ────────────────────────────────────────────────────────────


@router.post(
    "/",
    summary="Create or ingest a payment record",
    description=(
        "Create a payment record directly or by ingesting full gateway response data. "
        "Captures complete transaction details, financial charges, customer snapshot, "
        "and booking snapshot for financial auditing and dashboard reporting."
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
    parsed_gateway = {}
    if body.gateway_response:
        parsed_gateway = parse_gateway_response(body.gateway_response)

    # ── 1. Resolve booking, voucher & customer ────────────────────────
    booking_id_raw = (
        body.booking_id
        or parsed_gateway.get("customer_reference")
        or getattr(body, "booking_id", None)
    )

    booking = None
    booking_id = None
    if booking_id_raw:
        try:
            booking_id = uuid.UUID(str(booking_id_raw))
            stmt = select(Booking).where(Booking.id == booking_id)
            result = await session.execute(stmt)
            booking = result.scalar_one_or_none()
        except ValueError:
            pass

    # Resolve voucher_id & voucher_data
    voucher_id = None
    voucher_id_raw = getattr(body, "voucher_id", None)
    if voucher_id_raw:
        try:
            voucher_id = uuid.UUID(str(voucher_id_raw))
        except ValueError:
            pass
    voucher_data = dict(getattr(body, "voucher_data", None) or {})

    # Resolve payment_for
    payment_for_val = getattr(body, "payment_for", None)
    if payment_for_val:
        payment_for = str(payment_for_val)
    elif voucher_id or getattr(body, "voucher_id", None):
        payment_for = PaymentFor.GIFT_VOUCHER.value
    elif booking and getattr(booking, "booking_type", None) == "home":
        payment_for = PaymentFor.HOME_SERVICE.value
    elif booking:
        payment_for = PaymentFor.SERVICE.value
    else:
        payment_for = PaymentFor.OTHERS.value

    # Resolve customer ID — must come from body or booking (no JWT user available)
    customer_id = None
    if body.customer_id:
        try:
            customer_id = uuid.UUID(str(body.customer_id))
        except ValueError:
            pass
    if not customer_id and booking:
        customer_id = booking.customer_id
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="customer_id is required when calling this endpoint with an app token.",
        )

    # ── 2. Build customer & booking snapshot ──────────────────────────
    customer_data = dict(body.customer_data or {})
    if not customer_data and parsed_gateway.get("customer_data"):
        customer_data = parsed_gateway["customer_data"]
    if booking and booking.customer_data:
        customer_data = {**booking.customer_data, **customer_data}

    booking_data = dict(body.booking_data or {})
    if booking and not booking_data:
        booking_data = _build_booking_snapshot(booking)

    # ── 3. Resolve financial fields ───────────────────────────────────
    amount_val = body.amount or parsed_gateway.get("amount") or (booking.total_amount if booking else Decimal("0.000"))
    amount = Decimal(str(amount_val))

    currency = body.currency or parsed_gateway.get("currency") or (booking.currency if booking else "KWD")
    if currency in ("KD", "KWD"):
        currency = "KWD"

    service_charge_val = body.service_charge or parsed_gateway.get("service_charge") or Decimal("0.000")
    service_charge = Decimal(str(service_charge_val))

    vat_amount_val = body.vat_amount or parsed_gateway.get("vat_amount") or Decimal("0.000")
    vat_amount = Decimal(str(vat_amount_val))

    due_deposit_val = body.due_deposit or parsed_gateway.get("due_deposit")
    due_deposit = Decimal(str(due_deposit_val)) if due_deposit_val is not None else None

    deposit_status = body.deposit_status or parsed_gateway.get("deposit_status") or "Not Deposited"

    # ── 4. Resolve gateway identifiers ────────────────────────────────
    provider = body.provider or parsed_gateway.get("provider") or "myfatoorah"
    payment_method = body.payment_method or parsed_gateway.get("payment_method") or "card"
    status_str = body.status or parsed_gateway.get("status") or PaymentTransactionStatus.SUCCESS.value

    gateway_name = body.gateway_name or parsed_gateway.get("gateway_name")
    provider_payment_id = body.provider_payment_id or parsed_gateway.get("provider_payment_id")
    provider_reference = body.invoice_reference or parsed_gateway.get("provider_reference")
    provider_transaction_id = body.provider_transaction_id or parsed_gateway.get("provider_transaction_id")
    invoice_reference = body.invoice_reference or parsed_gateway.get("invoice_reference")
    customer_reference = body.customer_reference or parsed_gateway.get("customer_reference")
    reference_id = body.reference_id or parsed_gateway.get("reference_id")
    track_id = body.track_id or parsed_gateway.get("track_id")
    authorization_id = body.authorization_id or parsed_gateway.get("authorization_id")
    payment_id_gateway = parsed_gateway.get("payment_id_gateway")

    ip_address = parsed_gateway.get("ip_address")
    country = parsed_gateway.get("country")
    card_info = body.card_info or parsed_gateway.get("card_info")
    paid_at = parsed_gateway.get("paid_at") or (datetime.now() if status_str == PaymentTransactionStatus.SUCCESS.value else None)
    payment_url = parsed_gateway.get("payment_url")
    provider_response = body.gateway_response or parsed_gateway.get("provider_response")

    # ── 5. Resolve Unified Standard Fields ────────────────────────────
    final_payment_id = body.payment_id or parsed_gateway.get("payment_id") or payment_id_gateway or (str(provider_payment_id) if provider_payment_id else None)
    final_transaction_id = body.transaction_id or parsed_gateway.get("transaction_id") or provider_transaction_id or None
    is_paid_val = body.is_paid if body.is_paid is not None else (parsed_gateway.get("is_paid") if parsed_gateway.get("is_paid") is not None else (status_str == PaymentTransactionStatus.SUCCESS.value))
    final_invoice_id = body.invoice_id or parsed_gateway.get("invoice_id") or provider_payment_id or None
    invoice_value_raw = body.invoice_value or parsed_gateway.get("invoice_value") or amount
    invoice_value = Decimal(str(invoice_value_raw)) if invoice_value_raw is not None else amount

    final_customer_name = body.customer_name or parsed_gateway.get("customer_name") or customer_data.get("name") or None
    final_customer_mobile = body.customer_mobile or parsed_gateway.get("customer_mobile") or customer_data.get("mobile") or customer_data.get("phone_number") or None
    final_customer_email = body.customer_email or parsed_gateway.get("customer_email") or customer_data.get("email") or None

    created_date = body.created_date or parsed_gateway.get("created_date") or None
    transaction_date = body.transaction_date or parsed_gateway.get("transaction_date") or None
    payment_gateway = body.payment_gateway or parsed_gateway.get("payment_gateway") or gateway_name or None

    # ── 6. Create Payment record ──────────────────────────────────────
    payment = Payment(
        booking_id=booking_id,
        customer_id=customer_id,
        voucher_id=voucher_id,
        voucher_data=voucher_data or None,
        payment_for=payment_for,
        payment_id=final_payment_id,
        transaction_id=final_transaction_id,
        is_paid=is_paid_val,
        invoice_id=final_invoice_id,
        invoice_value=invoice_value,
        customer_name=final_customer_name,
        customer_mobile=final_customer_mobile,
        customer_email=final_customer_email,
        created_date=created_date,
        transaction_date=transaction_date,
        payment_gateway=payment_gateway,
        amount=amount,
        currency=currency,
        service_charge=service_charge,
        vat_amount=vat_amount,
        due_deposit=due_deposit,
        deposit_status=deposit_status,
        provider=provider,
        gateway_name=gateway_name,
        payment_method=payment_method,
        status=status_str,
        provider_payment_id=provider_payment_id,
        provider_reference=provider_reference,
        provider_transaction_id=provider_transaction_id,
        invoice_reference=invoice_reference,
        customer_reference=customer_reference,
        reference_id=reference_id,
        track_id=track_id,
        authorization_id=authorization_id,
        payment_id_gateway=payment_id_gateway,
        payment_url=payment_url,
        failure_reason=body.failure_reason,
        ip_address=ip_address,
        country=country,
        paid_at=paid_at,
        customer_data=customer_data,
        booking_data=booking_data,
        card_info=card_info,
        metadata_=body.metadata,
        provider_response=provider_response,
        idempotency_key=body.idempotency_key,
    )

    session.add(payment)
    await session.flush()
    await session.refresh(payment)

    # ── 6. Add initial status history ─────────────────────────────────
    history = PaymentStatusHistory(
        payment_id=payment.id,
        old_status=None,
        new_status=payment.status,
        source="gateway_webhook" if body.gateway_response else "manual",
        reason="Payment record created",
        provider_reference=payment.provider_reference,
        correlation_id=payment.provider_payment_id or str(payment.id),
        metadata_=payment.metadata_,
    )
    session.add(history)
    await session.flush()

    # ── 7. Update booking payments_meta & auto-confirm if SUCCESS ───
    if booking:
        payment_meta_snapshot = {
            "payment_id": payment.payment_id or str(payment.id),
            "transaction_id": payment.transaction_id,
            "is_paid": payment.is_paid,
            "invoice_id": payment.invoice_id,
            "invoice_value": str(payment.invoice_value or payment.amount),
            "status": payment.status,
            "invoice_reference": payment.invoice_reference,
            "customer_reference": payment.customer_reference,
            "created_date": payment.created_date,
            "customer_name": payment.customer_name,
            "customer_mobile": payment.customer_mobile,
            "customer_email": payment.customer_email,
            "transaction_date": payment.transaction_date,
            "payment_gateway": payment.payment_gateway or payment.gateway_name,
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
                    correlation_id=payment.provider_reference or payment.provider_payment_id or str(payment.id),
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
    payment_for_filter: str | None = Query(default=None, alias="payment_for"),
    customer_id: uuid.UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    is_paid_filter: bool | None = Query(default=None, alias="is_paid"),
    provider_filter: str | None = Query(default=None, alias="provider"),
    gateway_name_filter: str | None = Query(default=None, alias="gateway_name"),
    payment_gateway_filter: str | None = Query(default=None, alias="payment_gateway"),
    payment_id_filter: str | None = Query(default=None, alias="payment_id"),
    transaction_id_filter: str | None = Query(default=None, alias="transaction_id"),
    invoice_id_filter: str | None = Query(default=None, alias="invoice_id"),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    search: str | None = Query(default=None, description="Search by payment ID, transaction ID, invoice ID, customer name, mobile, email, etc."),
) -> JSONResponse:
    """List payments with filtering and financial analytics summary."""
    conditions = []

    if booking_id:
        conditions.append(Payment.booking_id == booking_id)
    if voucher_id:
        conditions.append(Payment.voucher_id == voucher_id)
    if payment_for_filter:
        conditions.append(Payment.payment_for == payment_for_filter)
    if customer_id:
        conditions.append(Payment.customer_id == customer_id)
    if status_filter:
        conditions.append(Payment.status == status_filter)
    if is_paid_filter is not None:
        conditions.append(Payment.is_paid == is_paid_filter)
    if provider_filter:
        conditions.append(Payment.provider == provider_filter)
    if gateway_name_filter:
        conditions.append(or_(Payment.gateway_name == gateway_name_filter, Payment.payment_gateway == gateway_name_filter))
    if payment_gateway_filter:
        conditions.append(or_(Payment.payment_gateway == payment_gateway_filter, Payment.gateway_name == payment_gateway_filter))
    if payment_id_filter:
        conditions.append(or_(Payment.payment_id == payment_id_filter, Payment.payment_id_gateway == payment_id_filter, Payment.provider_payment_id == payment_id_filter))
    if transaction_id_filter:
        conditions.append(or_(Payment.transaction_id == transaction_id_filter, Payment.provider_transaction_id == transaction_id_filter))
    if invoice_id_filter:
        conditions.append(or_(Payment.invoice_id == invoice_id_filter, Payment.provider_payment_id == invoice_id_filter))
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
                Payment.customer_name.ilike(search_pattern),
                Payment.customer_mobile.ilike(search_pattern),
                Payment.customer_email.ilike(search_pattern),
                Payment.payment_gateway.ilike(search_pattern),
                Payment.invoice_reference.ilike(search_pattern),
                Payment.reference_id.ilike(search_pattern),
                Payment.track_id.ilike(search_pattern),
                Payment.provider_payment_id.ilike(search_pattern),
                Payment.provider_reference.ilike(search_pattern),
                Payment.customer_reference.ilike(search_pattern),
            )
        )

    # Count query
    count_stmt = select(func.count(Payment.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total_count = (await session.execute(count_stmt)).scalar_one()

    # Aggregate financial metrics
    sum_stmt = select(
        func.coalesce(func.sum(Payment.amount), Decimal("0.000")).label("total_volume"),
        func.coalesce(func.sum(Payment.service_charge), Decimal("0.000")).label("total_service_charge"),
        func.coalesce(func.sum(Payment.vat_amount), Decimal("0.000")).label("total_vat"),
        func.coalesce(func.sum(Payment.due_deposit), Decimal("0.000")).label("total_due_deposit"),
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
        "total_service_charge": str(agg_res.total_service_charge),
        "total_vat": str(agg_res.total_vat),
        "total_due_deposit": str(agg_res.total_due_deposit),
    }

    return JSONResponse(content=out)


@router.get(
    "/{payment_id}/",
    summary="Get payment detail",
    description="Retrieve full payment record with transaction audit trail, financial details, and gateway response.",
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
    summary="Update payment",
    description="Partially update payment details, status, or deposit status.",
    response_model=PaymentResponse,
)
@router.put(
    "/{payment_id}/",
    summary="Update payment (full)",
    description="Update payment details.",
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
        payment.provider_response = body.gateway_response
        if parsed.get("status"):
            payment.status = parsed["status"]
        if parsed.get("service_charge"):
            payment.service_charge = parsed["service_charge"]
        if parsed.get("vat_amount"):
            payment.vat_amount = parsed["vat_amount"]
        if parsed.get("due_deposit"):
            payment.due_deposit = parsed["due_deposit"]
        if parsed.get("deposit_status"):
            payment.deposit_status = parsed["deposit_status"]
        if parsed.get("gateway_name"):
            payment.gateway_name = parsed["gateway_name"]
        if parsed.get("reference_id"):
            payment.reference_id = parsed["reference_id"]
        if parsed.get("track_id"):
            payment.track_id = parsed["track_id"]
        if parsed.get("authorization_id"):
            payment.authorization_id = parsed["authorization_id"]
        if parsed.get("card_info"):
            payment.card_info = parsed["card_info"]
        if parsed.get("paid_at"):
            payment.paid_at = parsed["paid_at"]
        if parsed.get("payment_id"):
            payment.payment_id = parsed["payment_id"]
        if parsed.get("transaction_id"):
            payment.transaction_id = parsed["transaction_id"]
        if parsed.get("is_paid") is not None:
            payment.is_paid = parsed["is_paid"]
        if parsed.get("invoice_id"):
            payment.invoice_id = parsed["invoice_id"]
        if parsed.get("invoice_value"):
            payment.invoice_value = parsed["invoice_value"]
        if parsed.get("customer_name"):
            payment.customer_name = parsed["customer_name"]
        if parsed.get("customer_mobile"):
            payment.customer_mobile = parsed["customer_mobile"]
        if parsed.get("customer_email"):
            payment.customer_email = parsed["customer_email"]
        if parsed.get("created_date"):
            payment.created_date = parsed["created_date"]
        if parsed.get("transaction_date"):
            payment.transaction_date = parsed["transaction_date"]
        if parsed.get("payment_gateway"):
            payment.payment_gateway = parsed["payment_gateway"]

    if body.status:
        payment.status = body.status
    if body.is_paid is not None:
        payment.is_paid = body.is_paid
    elif payment.status == "success":
        payment.is_paid = True
    if body.payment_id is not None:
        payment.payment_id = body.payment_id
    if body.transaction_id is not None:
        payment.transaction_id = body.transaction_id
    if body.invoice_id is not None:
        payment.invoice_id = body.invoice_id
    if body.invoice_value is not None:
        payment.invoice_value = Decimal(str(body.invoice_value))
    if body.customer_name is not None:
        payment.customer_name = body.customer_name
    if body.customer_mobile is not None:
        payment.customer_mobile = body.customer_mobile
    if body.customer_email is not None:
        payment.customer_email = body.customer_email
    if body.created_date is not None:
        payment.created_date = body.created_date
    if body.transaction_date is not None:
        payment.transaction_date = body.transaction_date
    if body.payment_gateway is not None:
        payment.payment_gateway = body.payment_gateway
    if body.invoice_reference is not None:
        payment.invoice_reference = body.invoice_reference
    if body.customer_reference is not None:
        payment.customer_reference = body.customer_reference
    if body.failure_reason is not None:
        payment.failure_reason = body.failure_reason
    if body.deposit_status is not None:
        payment.deposit_status = body.deposit_status
    if body.due_deposit is not None:
        payment.due_deposit = Decimal(str(body.due_deposit))
    if body.service_charge is not None:
        payment.service_charge = Decimal(str(body.service_charge))
    if body.vat_amount is not None:
        payment.vat_amount = Decimal(str(body.vat_amount))
    if body.customer_data is not None:
        payment.customer_data = {**(payment.customer_data or {}), **body.customer_data}
    if body.booking_data is not None:
        payment.booking_data = {**(payment.booking_data or {}), **body.booking_data}
    if body.booking_id is not None:
        try:
            payment.booking_id = uuid.UUID(str(body.booking_id)) if body.booking_id else None
        except ValueError:
            pass
    if body.voucher_id is not None:
        try:
            payment.voucher_id = uuid.UUID(str(body.voucher_id)) if body.voucher_id else None
        except ValueError:
            pass
    if body.voucher_data is not None:
        payment.voucher_data = {**(payment.voucher_data or {}), **body.voucher_data}
    if body.payment_for is not None:
        payment.payment_for = str(body.payment_for)
    if body.metadata is not None:
        payment.metadata_ = {**(payment.metadata_ or {}), **body.metadata}

    # Record status change in audit trail
    if payment.status != old_status:
        history = PaymentStatusHistory(
            payment_id=payment.id,
            old_status=old_status,
            new_status=payment.status,
            source=body.source,
            reason=body.reason or "Payment updated",
            provider_reference=payment.provider_reference,
            correlation_id=payment.provider_payment_id or str(payment.id),
            metadata_=payment.metadata_,
        )
        session.add(history)

        # Transition booking if payment status changed to success
        if payment.status == PaymentTransactionStatus.SUCCESS.value and payment.booking_id:
            payment_meta_snapshot = {
                "payment_id": payment.payment_id or str(payment.id),
                "transaction_id": payment.transaction_id,
                "is_paid": payment.is_paid,
                "invoice_id": payment.invoice_id,
                "invoice_value": str(payment.invoice_value or payment.amount),
                "status": payment.status,
                "invoice_reference": payment.invoice_reference,
                "customer_reference": payment.customer_reference,
                "created_date": payment.created_date,
                "customer_name": payment.customer_name,
                "customer_mobile": payment.customer_mobile,
                "customer_email": payment.customer_email,
                "transaction_date": payment.transaction_date,
                "payment_gateway": payment.payment_gateway or payment.gateway_name,
                "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            }
            try:
                booking_service = BookingService(session=session, settings=settings)
                await booking_service.confirm_booking(
                    payment.booking_id,
                    payment_id=str(payment.id),
                    payments_meta=payment_meta_snapshot,
                    correlation_id=payment.provider_reference or payment.provider_payment_id or str(payment.id),
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

    repo = BookingRepository(session)

    # Validate booking ownership and state
    try:
        booking = await repo.get_by_id(booking_id, for_update=True)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found.")

    if booking.customer_id != customer_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    if booking.status not in (
        BookingStatus.REQUESTED.value,
        BookingStatus.PAYMENT_FAILED.value,
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot initiate payment for booking with status '{booking.status}'.",
        )

    # ── Create payment record with rich snapshots ─────────────────────
    customer_dict = booking.customer_data or {}
    service_dict = booking.service_data or {}
    booking_snapshot = _build_booking_snapshot(booking)

    payment = Payment(
        booking_id=booking.id,
        customer_id=customer_id,
        amount=booking.total_amount,
        currency=booking.currency,
        provider=body.provider.value,
        payment_method=body.payment_method,
        status=PaymentTransactionStatus.INITIATED.value,
        customer_data=customer_dict,
        booking_data=booking_snapshot,
    )
    session.add(payment)
    await session.flush()
    await session.refresh(payment)

    # Record status history
    session.add(
        PaymentStatusHistory(
            payment_id=payment.id,
            old_status=None,
            new_status=PaymentTransactionStatus.INITIATED.value,
            source="customer",
            reason="Payment session initiated",
            correlation_id=str(payment.id),
        )
    )

    # ── Update booking to PAYMENT_PENDING ─────────────────────────────
    from app.booking.domain.state_machine import BookingStateMachine

    machine = BookingStateMachine(BookingStatus(booking.status))
    machine.transition_to(BookingStatus.PAYMENT_PENDING)
    booking.status = BookingStatus.PAYMENT_PENDING.value
    await session.flush()

    # ── Call payment provider ──────────────────────────────────────────
    http_client = get_http_client()
    provider = _get_provider(body.provider, http_client, settings)

    callback_url = ""
    if body.provider == PaymentProvider.MYFATOORAH:
        callback_url = settings.MYFATOORAH_CALLBACK_URL
    elif body.provider == PaymentProvider.TAP:
        callback_url = settings.TAP_CALLBACK_URL

    cust_name = (
        customer_dict.get("name")
        or f"{customer_dict.get('first_name', '')} {customer_dict.get('last_name', '')}".strip()
        or customer_dict.get("phone_number")
        or customer_dict.get("email")
        or "Customer"
    )
    svc_name = service_dict.get("name") or "Service"

    pay_request = CreatePaymentRequest(
        booking_id=str(booking.id),
        customer_id=str(customer_id),
        amount=booking.total_amount,
        currency=booking.currency,
        payment_method=body.payment_method,
        customer_name=cust_name,
        customer_email=str(customer_dict.get("email") or ""),
        customer_phone=str(customer_dict.get("phone_number") or customer_dict.get("phone") or ""),
        description=f"Booking #{str(booking.id)[:8]} - {svc_name}",
        callback_url=callback_url,
        success_url=settings.MYFATOORAH_SUCCESS_URL,
        error_url=settings.MYFATOORAH_ERROR_URL,
        metadata={"booking_id": str(booking.id), "payment_id": str(payment.id)},
    )

    try:
        response = await provider.create_payment(pay_request)  # type: ignore
    except PaymentProviderError as exc:
        logger.error("payment_initiation_failed", booking_id=str(booking.id), error=exc.message)
        payment.status = PaymentTransactionStatus.FAILED.value
        payment.failure_reason = exc.message
        raise HTTPException(status_code=502, detail="Payment provider error. Please try again.")

    # ── Update payment record with provider reference ──────────────────
    payment.provider_payment_id = response.provider_payment_id
    payment.provider_reference = response.provider_reference
    payment.payment_url = response.payment_url
    payment.status = PaymentTransactionStatus.PENDING.value
    await session.flush()

    logger.info(
        "payment_initiated",
        payment_id=str(payment.id),
        booking_id=str(booking.id),
        provider=body.provider.value,
    )

    return JSONResponse(
        status_code=201,
        content={
            "success": True,
            "data": {
                "payment_id": str(payment.id),
                "booking_id": str(booking.id),
                "provider": body.provider.value,
                "payment_url": response.payment_url,
                "amount": str(payment.amount),
                "currency": payment.currency,
            },
        },
    )


@router.get(
    "/{booking_id}/status/",
    summary="Get payment status for a booking",
)
async def get_payment_status(
    booking_id: uuid.UUID,
    current_user: CurrentUser,
    session: DBSession,
) -> JSONResponse:
    """Retrieve the latest payment record for a booking."""
    customer_id = uuid.UUID(current_user.sub)

    # Verify booking ownership
    repo = BookingRepository(session)
    try:
        booking = await repo.get_by_id(booking_id)
    except BookingNotFoundError:
        raise HTTPException(status_code=404, detail="Booking not found.")

    if booking.customer_id != customer_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    stmt = (
        select(Payment)
        .where(Payment.booking_id == booking_id)
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if payment is None:
        return JSONResponse(
            content={"success": True, "data": None, "meta": {"message": "No payment initiated."}}
        )

    return JSONResponse(
        content={
            "success": True,
            "data": {
                "payment_id": str(payment.id),
                "booking_id": str(booking_id),
                "provider": payment.provider,
                "status": payment.status,
                "amount": str(payment.amount),
                "currency": payment.currency,
                "payment_method": payment.payment_method,
                "provider_reference": payment.provider_reference,
                "created_at": payment.created_at.isoformat(),
                "updated_at": payment.updated_at.isoformat(),
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
    session: DBSession,
    settings: AppSettings,
) -> JSONResponse:
    """
    MyFatoorah payment webhook.

    Called by MyFatoorah after payment completion.
    Verifies payment status, stores rich audit attributes, and confirms booking.
    """
    from app.api.deps import get_http_client

    payload = await request.json()
    invoice_id = str(payload.get("InvoiceId") or payload.get("invoiceId") or "")
    logger.info("myfatoorah_webhook_received", invoice_id=invoice_id)

    if not invoice_id:
        return JSONResponse(content={"received": True})

    # Find the payment record
    stmt = select(Payment).where(
        or_(
            Payment.provider_payment_id == invoice_id,
            Payment.provider_reference == invoice_id,
        )
    )
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        logger.warning("myfatoorah_webhook_payment_not_found", invoice_id=invoice_id)
        return JSONResponse(content={"received": True})

    old_status = payment.status

    # Verify with MyFatoorah API
    provider = MyFatoorahProvider(http_client=get_http_client(), settings=settings)
    verify = await provider.verify_payment(invoice_id)

    # Ingest full verified raw response
    parsed = parse_gateway_response(verify.raw_response or payload)
    payment.provider_response = verify.raw_response or payload

    if parsed.get("service_charge"):
        payment.service_charge = parsed["service_charge"]
    if parsed.get("vat_amount"):
        payment.vat_amount = parsed["vat_amount"]
    if parsed.get("due_deposit"):
        payment.due_deposit = parsed["due_deposit"]
    if parsed.get("deposit_status"):
        payment.deposit_status = parsed["deposit_status"]
    if parsed.get("gateway_name"):
        payment.gateway_name = parsed["gateway_name"]
    if parsed.get("reference_id"):
        payment.reference_id = parsed["reference_id"]
    if parsed.get("track_id"):
        payment.track_id = parsed["track_id"]
    if parsed.get("authorization_id"):
        payment.authorization_id = parsed["authorization_id"]
    if parsed.get("provider_transaction_id"):
        payment.provider_transaction_id = parsed["provider_transaction_id"]
    if parsed.get("payment_id_gateway"):
        payment.payment_id_gateway = parsed["payment_id_gateway"]
    if parsed.get("invoice_reference"):
        payment.invoice_reference = parsed["invoice_reference"]
    if parsed.get("card_info"):
        payment.card_info = parsed["card_info"]
    if parsed.get("paid_at"):
        payment.paid_at = parsed["paid_at"]

    if verify.is_successful:
        payment.status = PaymentTransactionStatus.SUCCESS.value
        await session.flush()

        # Add status history
        session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status,
                new_status=payment.status,
                source="myfatoorah_webhook",
                reason="Payment verified successfully",
                provider_reference=invoice_id,
                correlation_id=invoice_id,
            )
        )

        # Confirm booking
        if payment.booking_id:
            booking_service = BookingService(session=session, settings=settings)
            await booking_service.confirm_booking(
                payment.booking_id,
                payment_id=str(payment.id),
                correlation_id=invoice_id,
            )
    else:
        payment.status = PaymentTransactionStatus.FAILED.value
        payment.failure_reason = verify.failure_reason or "Payment not completed"
        await session.flush()

        session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status,
                new_status=payment.status,
                source="myfatoorah_webhook",
                reason=payment.failure_reason,
                provider_reference=invoice_id,
                correlation_id=invoice_id,
            )
        )

        # Update booking to payment_failed
        if payment.booking_id:
            repo = BookingRepository(session)
            try:
                booking = await repo.get_by_id(payment.booking_id, for_update=True)
                from app.booking.domain.state_machine import BookingStateMachine
                machine = BookingStateMachine(BookingStatus(booking.status))
                machine.transition_to(BookingStatus.PAYMENT_FAILED)
                booking.status = BookingStatus.PAYMENT_FAILED.value
                await session.flush()
            except Exception:
                pass

    return JSONResponse(content={"received": True})


@router.post(
    "/webhook/tap/",
    summary="Tap payment webhook",
    include_in_schema=False,
)
async def tap_webhook(
    request: Request,
    session: DBSession,
    settings: AppSettings,
) -> JSONResponse:
    """
    Tap Payments webhook.

    Called by Tap after payment completion/failure.
    """
    from app.api.deps import get_http_client

    payload = await request.json()
    charge_id = str(payload.get("id", ""))
    logger.info("tap_webhook_received", charge_id=charge_id)

    if not charge_id:
        return JSONResponse(content={"received": True})

    stmt = select(Payment).where(
        or_(
            Payment.provider_payment_id == charge_id,
            Payment.provider_reference == charge_id,
        )
    )
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        logger.warning("tap_webhook_payment_not_found", charge_id=charge_id)
        return JSONResponse(content={"received": True})

    old_status = payment.status
    provider = TapProvider(http_client=get_http_client(), settings=settings)
    verify = await provider.verify_payment(charge_id)

    payment.provider_response = verify.raw_response or payload

    if verify.is_successful:
        payment.status = PaymentTransactionStatus.SUCCESS.value
        await session.flush()

        session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status,
                new_status=payment.status,
                source="tap_webhook",
                reason="Payment verified successfully",
                provider_reference=charge_id,
                correlation_id=charge_id,
            )
        )

        if payment.booking_id:
            booking_service = BookingService(session=session, settings=settings)
            await booking_service.confirm_booking(
                payment.booking_id,
                payment_id=str(payment.id),
                correlation_id=charge_id,
            )
    else:
        payment.status = PaymentTransactionStatus.FAILED.value
        payment.failure_reason = verify.failure_reason or "Payment failed"
        await session.flush()

        session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_status,
                new_status=payment.status,
                source="tap_webhook",
                reason=payment.failure_reason,
                provider_reference=charge_id,
                correlation_id=charge_id,
            )
        )

    return JSONResponse(content={"received": True})
