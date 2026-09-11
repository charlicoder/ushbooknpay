"""
app/api/v1/bookings.py
───────────────────────
Booking API endpoints.

Routes:
  POST   /api/v1/bookings/                    → Create booking
  GET    /api/v1/bookings/                    → List customer bookings
  GET    /api/v1/bookings/{booking_id}/       → Get booking detail
  POST   /api/v1/bookings/{booking_id}/cancel/       → Cancel booking
  POST   /api/v1/bookings/{booking_id}/reschedule/   → Request reschedule
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import AppSettings, BookingServiceDep, CurrentUser, DBSession, RequireAppToken, USHAuthDep
from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
from app.booking.interfaces.schemas import (
    AddonSchema,
    BookingDetailResponse,
    BookingListItem,
    BookingListResponse,
    CancelBookingRequest,
    CreateBookingDataResponse,
    CreateBookingRequest,
    CreateBookingResponse,
    PricingBreakdownSchema,
    RescheduleRequest,
    StatusHistoryItem,
    UpdateBookingRequest,
    UpdateBookingResponse,
    UpdateBookingStatusRequest,
)
from app.common.pagination import make_paginated_response
from app.core.exceptions import (
    AuthorizationError,
    BookingNotFoundError,
    DoubleBookingError,
    GatewayError,
    GatewayTimeoutError,
    NotFoundError,
    USHBaseError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/bookings", tags=["Bookings"])


def _booking_to_list_item(b: object) -> BookingListItem:
    """Map ORM Booking to BookingListItem."""
    b_type = getattr(b, "booking_type", "branch")
    booking_type_val = b_type if isinstance(b_type, str) else "branch_service"
    raw_base_price = getattr(b, "base_price", None)
    raw_app_date = getattr(b, "appointment_date", None) or getattr(b, "appointment_start", None)
    return BookingListItem(
        id=str(b.id),
        customer_id=str(b.customer_id),
        customer_data=b.customer_data,
        branch_id=str(b.branch_id) if b.branch_id else None,
        branch_data=b.branch_data,
        service_id=str(b.service_id),
        service_data=b.service_data,
        service_arrangement_id=str(b.service_arrangement_id) if b.service_arrangement_id else None,
        service_arrangement_data=b.service_arrangement_data,
        therapist_id=str(b.therapist_id),
        therapist_data=b.therapist_data,
        appointment_date=raw_app_date,
        appointment_start=b.appointment_start,
        appointment_end=b.appointment_end,
        duration_minutes=b.duration_minutes,
        extra_minutes=b.extra_minutes,
        total_duration=getattr(b, "total_duration", None),
        addons_duration=getattr(b, "addons_duration", None),
        base_price=str(raw_base_price) if raw_base_price is not None else None,
        booking_type=booking_type_val,
        payment_type=getattr(b, "payment_type", "service") or "service",
        status=b.status,
        payment_status=b.payment_status,
        payment_data=b.payment_data or {},
        total_amount=str(b.total_amount),
        currency=b.currency,
        is_eligible_for_loyalty=bool((b.service_data or {}).get("is_eligible_for_loyalty", False)),
        loyalty_data=getattr(b, "loyalty_data", None) if isinstance(getattr(b, "loyalty_data", None), dict) else None,
        reward_id=str(b.reward_id) if getattr(b, "reward_id", None) and isinstance(getattr(b, "reward_id", None), (str, uuid.UUID)) else None,
        voucher_id=str(b.voucher_id) if getattr(b, "voucher_id", None) and isinstance(getattr(b, "voucher_id", None), (str, uuid.UUID)) else None,
        voucher_data=getattr(b, "voucher_data", None) if isinstance(getattr(b, "voucher_data", None), dict) else None,
        created_at=b.created_at,
        created_by=str(getattr(b, "created_by", None) or "") or None,
    )


def _booking_to_detail(booking: object) -> BookingDetailResponse:
    """Map ORM Booking to BookingDetailResponse."""
    b = booking
    addons = []
    for addon in (b.addons or []):
        addons.append(AddonSchema(
            addon_id=addon.get("addon_id", ""),
            name=addon.get("name", ""),
            price=str(addon.get("price", "0")),
        ))

    history = None
    raw_history = b.__dict__.get("status_history")
    if raw_history:
        history = [
            StatusHistoryItem(
                old_status=h.old_status,
                new_status=h.new_status,
                source=h.source,
                reason=h.reason,
                created_at=h.created_at,
            )
            for h in sorted(raw_history, key=lambda x: x.created_at)
        ]

    # Always include is_eligible_for_loyalty inside service_data so the
    # client receives a consistent shape regardless of when the booking was created.
    _raw_service_data = b.service_data or {}
    is_eligible = bool(_raw_service_data.get("is_eligible_for_loyalty", False))
    service_data_out = {**_raw_service_data, "is_eligible_for_loyalty": is_eligible}

    raw_base_price = getattr(b, "base_price", None)
    raw_app_date = getattr(b, "appointment_date", None) or getattr(b, "appointment_start", None)
    return BookingDetailResponse(
        id=str(b.id),
        customer_id=str(b.customer_id),
        customer_data=b.customer_data,
        branch_id=str(b.branch_id) if b.branch_id else None,
        branch_data=b.branch_data,
        service_id=str(b.service_id),
        service_data=service_data_out,
        service_arrangement_id=str(b.service_arrangement_id) if b.service_arrangement_id else None,
        service_arrangement_data=b.service_arrangement_data,
        therapist_id=str(b.therapist_id),
        therapist_data=b.therapist_data,
        appointment_date=raw_app_date,
        appointment_start=b.appointment_start,
        appointment_end=b.appointment_end,
        duration_minutes=b.duration_minutes,
        extra_minutes=b.extra_minutes,
        total_duration=getattr(b, "total_duration", None),
        addons_duration=getattr(b, "addons_duration", None),
        base_price=str(raw_base_price) if raw_base_price is not None else None,
        price_for_extra_minutes=str(b.price_for_extra_minutes),
        booking_type=getattr(b, "booking_type", "branch_service") or "branch_service",
        payment_type=getattr(b, "payment_type", "service") or "service",
        status=b.status,
        payment_status=b.payment_status,
        payment_data=b.payment_data or {},
        pricing=PricingBreakdownSchema(
            arrangement_price=str(b.arrangement_price),
            price_for_extra_minutes=str(b.price_for_extra_minutes),
            addon_price=str(b.addon_price),
            discount=str(b.discount),
            tax=str(b.tax),
            fees=str(b.fees),
            total=str(b.total_amount),
            currency=b.currency,
        ),
        addons=addons,
        customer_notes=b.customer_notes,
        internal_notes=b.internal_notes,
        status_history=history,
        is_eligible_for_loyalty=is_eligible,
        loyalty_data=getattr(b, "loyalty_data", None) if isinstance(getattr(b, "loyalty_data", None), dict) else None,
        reward_id=str(b.reward_id) if getattr(b, "reward_id", None) and isinstance(getattr(b, "reward_id", None), (str, uuid.UUID)) else None,
        voucher_id=str(b.voucher_id) if getattr(b, "voucher_id", None) and isinstance(getattr(b, "voucher_id", None), (str, uuid.UUID)) else None,
        voucher_data=getattr(b, "voucher_data", None) if isinstance(getattr(b, "voucher_data", None), dict) else None,
        created_at=b.created_at,
        updated_at=b.updated_at,
        created_by=str(getattr(b, "created_by", None) or "") or None,
    )


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateBookingResponse,
    summary="Create a new booking",
    description=(
        "Create a booking for an authenticated customer. "
        "Checks appointment availability with ushauth, assigns the available therapist, and creates the booking."
    ),
)
async def create_booking(
    request: Request,
    body: CreateBookingRequest,
    current_user: CurrentUser,
    booking_service: BookingServiceDep,
    ushauth: USHAuthDep,
    settings: AppSettings,
) -> CreateBookingResponse:
    """Create a new booking."""
    # ── Always track who made the request ───────────────────────────────
    created_by: str = current_user.sub

    # ── Resolve customer_id and customer_data ────────────────────────────
    # If the caller supplies a customer_id in the body, fetch that customer's
    # data from ushauth and use it as the booking customer.
    # Otherwise, the authenticated user is the customer (existing behavior).
    if body.customer_id is not None:
        try:
            raw_customer = await ushauth.get_customer_profile(str(body.customer_id))
            # ushauth returns the customer profile; normalise to a flat dict
            cust_profile = raw_customer.get("data", raw_customer)
            cust_user = cust_profile.get("user", {})
            customer_id = uuid.UUID(str(body.customer_id))
            customer_data = {
                "id": str(customer_id),
                "first_name": cust_profile.get("first_name") or cust_user.get("first_name") or "",
                "last_name": cust_profile.get("last_name") or cust_user.get("last_name") or "",
                "phone_number": (
                    cust_profile.get("phone_number")
                    or cust_user.get("phone_number")
                    or cust_user.get("mobile")
                    or ""
                ),
                "email": cust_profile.get("email") or cust_user.get("email") or "",
            }
        except Exception as exc:
            logger.warning(
                "create_booking_customer_fetch_failed",
                customer_id=str(body.customer_id),
                error=str(exc),
            )
            # Fall back to the requesting user's data
            customer_id = uuid.UUID(current_user.sub)
            customer_data = {
                "id": str(customer_id),
                "first_name": current_user.first_name or "",
                "last_name": current_user.last_name or "",
                "phone_number": current_user.phone_number or "",
                "email": current_user.email or "",
            }
    else:
        # No customer_id supplied — book on behalf of the authenticated user
        customer_id = uuid.UUID(current_user.sub)
        customer_data = {
            "id": str(customer_id),
            "first_name": current_user.first_name or "",
            "last_name": current_user.last_name or "",
            "phone_number": current_user.phone_number or "",
            "email": current_user.email or "",
        }

    # ── 1. Resolve date, time, duration & therapist IDs ─────────────────
    # appointment_date / appointment_time are now declared fields in the schema
    # with validators that sanitize milliseconds and timezone suffixes.
    # Fall back to legacy date / time_slot / appointment_start fields.
    appointment_date: str = (
        body.appointment_date
        or body.date
        or (body.appointment_start.strftime("%Y-%m-%d") if body.appointment_start else "")
    )
    # Strip any residual time/tz suffix (safety net)
    appointment_date = str(appointment_date).split("T")[0].strip()

    raw_time: str = (
        body.appointment_time
        or body.start_time
        or body.time_slot
        or (body.appointment_start.strftime("%H:%M:%S") if body.appointment_start else "")
    )
    # Schema validator already cleaned appointment_time/time_slot; apply
    # the same cleaning to appointment_start fallback just in case.
    raw_time_str = str(raw_time).strip()
    if raw_time_str.endswith("Z"):
        raw_time_str = raw_time_str[:-1]
    raw_time_str = re.sub(r"[+-]\d{2}:\d{2}$", "", raw_time_str)
    raw_time_str = raw_time_str.split(".")[0]
    appointment_time: str = (
        raw_time_str if len(raw_time_str.split(":")) == 3 else f"{raw_time_str}:00"
    )

    duration = int(body.total_duration or body.base_duration or 60)
    booking_type_str = (
        body.booking_type.value
        if hasattr(body.booking_type, "value")
        else str(body.booking_type or "branch_service")
    )

    # Auto-derive payment_type from booking_type when not explicitly provided
    if body.payment_type:
        payment_type_str = body.payment_type
    elif booking_type_str == "gift_voucher":
        payment_type_str = "gift_voucher"
    elif booking_type_str == "loyalty":
        payment_type_str = "rewarded"
    else:
        payment_type_str = "service"

    service_arrangement_id = str(body.service_arrangement_id) if body.service_arrangement_id else None

    therapist_id_str = str(body.therapist_id) if body.therapist_id else None
    raw_therapist_ids = (
        body.selected_therapist_ids
        or body.therapist_ids
        or ([body.therapist_id] if body.therapist_id else [])
    )
    therapist_ids = [str(tid) for tid in raw_therapist_ids if tid]
    if not therapist_id_str and therapist_ids:
        therapist_id_str = therapist_ids[0]

    # ── 2. Check appointment availability in ushauth microservice ───────
    # branch → check-appointment-availability (validates arrangement + therapist)
    # home   → check-booking-therapists-availability (validates therapist only)
    selected_therapist_id: uuid.UUID | None = None

    if booking_type_str in ("branch_service", "branch") and service_arrangement_id:
        # ── Branch booking: arrangement-level availability check ─────────
        try:
            avail_res = await ushauth.check_appointment_availability(
                service_arrangement_id=service_arrangement_id,
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                duration=duration,
                therapist_id=therapist_id_str,
                therapist_ids=therapist_ids if therapist_ids else None,
            )
        except GatewayTimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Availability check timed out: {str(exc)}",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("check_availability_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to check appointment availability: {str(exc)}",
            )

        if avail_res.get("available") != "yes":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The selected service arrangement or therapist is not available for the requested date and time.",
            )

        selected_therapist_id_str = avail_res.get("therapist_id")
        if not selected_therapist_id_str:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No available therapist was found for the requested time slot.",
            )

        try:
            selected_therapist_id = uuid.UUID(str(selected_therapist_id_str))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Invalid therapist ID received from availability service: {selected_therapist_id_str}",
            )

    elif booking_type_str in ("home_service", "home"):
        # ── Home booking: therapist-only availability check ──────────────
        if not therapist_id_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="therapistId is required for home bookings.",
            )

        try:
            avail_res = await ushauth.check_booking_therapist_availability(
                therapist_id=therapist_id_str,
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                duration=duration,
            )
        except GatewayTimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Therapist availability check timed out: {str(exc)}",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("check_home_availability_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to check therapist availability: {str(exc)}",
            )

        if avail_res.get("available") != "yes":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The selected therapist is not available for the requested date and time.",
            )

        try:
            selected_therapist_id = uuid.UUID(therapist_id_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid therapistId: {therapist_id_str}",
            )

    else:
        # ── Fallback: no arrangement and not explicitly home ─────────────
        # Use the therapist provided by the client without an availability check.
        if not therapist_id_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="therapistId is required when no service arrangement is provided.",
            )
        try:
            selected_therapist_id = uuid.UUID(therapist_id_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid therapistId: {therapist_id_str}",
            )

    # ── 4. Build start and end datetime ─────────────────────────────────
    try:
        naive_dt = datetime.fromisoformat(f"{appointment_date}T{appointment_time}")
        appointment_start = naive_dt.replace(tzinfo=timezone.utc)
    except Exception:
        appointment_start = body.appointment_start or datetime.now(timezone.utc)
    appointment_end = appointment_start + timedelta(minutes=duration)

    # ── 5. Build pricing and addons ─────────────────────────────────────
    if body.pricing_details:
        arr_price = str(
            body.pricing_details.get("arrangement_price")
            or body.pricing_details.get("arrangement")
            or body.pricing_details.get("base_price")
            or body.pricing_details.get("base")
            or body.base_price
            or "0"
        )
        add_price = str(
            body.pricing_details.get("addons_price")
            or body.pricing_details.get("addons")
            or body.addons_price
            or "0"
        )
        ext_price = str(
            body.pricing_details.get("extra_time_price")
            or body.pricing_details.get("extratime")
            or body.extra_price
            or "0"
        )
        tot_price = str(
            body.pricing_details.get("total_price")
            or body.pricing_details.get("total")
            or body.total_price
            or "0"
        )
        curr = str(body.pricing_details.get("currency") or body.currency or "KWD")
    else:
        arr_price = str(body.base_price or body.total_price or "0")
        add_price = str(body.addons_price or "0")
        ext_price = str(body.extra_price or "0")
        tot_price = str(body.total_price or body.base_price or "0")
        curr = body.currency or "KWD"

    pricing = PricingBreakdown(
        arrangement_price=Decimal(arr_price),
        price_for_extra_minutes=Decimal(ext_price),
        addon_price=Decimal(add_price),
        discount=Decimal("0.000"),
        tax=Decimal("0.000"),
        fees=Decimal("0.000"),
        total=Decimal(tot_price),
        currency=curr,
    )

    addon_records = []
    if body.selected_addons:
        for a in body.selected_addons:
            if isinstance(a, dict):
                addon_records.append({
                    "addon_id": str(a.get("id") or a.get("addon_id") or ""),
                    "name": a.get("name", ""),
                    "price": str(a.get("price", "0")),
                })
            elif hasattr(a, "id"):
                addon_records.append({
                    "addon_id": str(getattr(a, "id", "")),
                    "name": getattr(a, "name", ""),
                    "price": str(getattr(a, "price", "0")),
                })

    # ── 6. Assemble metadata ────────────────────────────────────────────
    # customer_data is already resolved above (from body.customer_id or current_user).

    branch_name = (
        body.branch_name
        or (body.branch_data.get("branch_name") or body.branch_data.get("name") if body.branch_data else "")
    )
    branch_address = (
        body.branch_address
        or (body.branch_data.get("branch_address") or body.branch_data.get("address") if body.branch_data else "")
    )
    branch_data = {
        "id": str(body.branch_id) if body.branch_id else "",
        "name": branch_name,
        "address": branch_address,
        "branch_name": branch_name,
        "branch_address": branch_address,
        **(body.branch_data or {}),
    }

    service_name = (
        body.service_name
        or (body.service_data.get("service_name") or body.service_data.get("name") if body.service_data else "")
    )
    service_category = (
        body.service_category
        or (body.service_data.get("service_category") or body.service_data.get("category") if body.service_data else "")
    )
    service_base_price = str(
        body.base_price
        or (body.service_data.get("base_price") if body.service_data else "0")
        or "0"
    )
    service_base_duration = int(
        body.base_duration
        or (body.service_data.get("base_duration") or body.service_data.get("duration") if body.service_data else 60)
        or 60
    )
    service_data = {
        "id": str(body.service_id) if body.service_id else "",
        "name": service_name,
        "category": service_category,
        "base_price": service_base_price,
        "base_duration": service_base_duration,
        **(body.service_data or {}),
        # Ensure is_eligible_for_loyalty is always explicitly stored.
        # Top-level request field takes precedence; falls back to whatever
        # the client may have nested inside service_data.
        "is_eligible_for_loyalty": bool(
            body.is_eligible_for_loyalty
            or (body.service_data or {}).get("is_eligible_for_loyalty", False)
        ),
    }

    arrangement_type = (
        body.arrangement_type
        or (body.service_arrangement_data.get("arrangement_type") or body.service_arrangement_data.get("type") if body.service_arrangement_data else "")
    )
    room_name = (
        body.room_name
        or body.arrangement_name
        or (body.service_arrangement_data.get("arrangement_name") or body.service_arrangement_data.get("room_name") or body.service_arrangement_data.get("name") if body.service_arrangement_data else "")
    )
    service_arrangement_data = {
        "id": str(body.service_arrangement_id) if body.service_arrangement_id else "",
        "arrangement_type": arrangement_type,
        "room_name": room_name,
        "arrangement_name": room_name,
        **(body.service_arrangement_data or {}),
    }

    therapist_name = (
        body.therapist_name
        or (body.therapist_data.get("therapist_name") or body.therapist_data.get("name") if body.therapist_data else "")
    )
    therapist_data = {
        "id": str(selected_therapist_id),
        "name": therapist_name,
        "therapist_name": therapist_name,
        **(body.therapist_data or {}),
    }

    idempotency_key = body.idempotency_key or request.headers.get("Idempotency-Key")

    # ── 7. Create booking record ────────────────────────────────────────
    try:
        booking = await booking_service.create_booking(
            customer_id=customer_id,
            customer_data=customer_data,
            branch_id=body.branch_id,
            branch_data=branch_data,
            service_id=body.service_id or uuid.uuid4(),
            service_data=service_data,
            service_arrangement_id=body.service_arrangement_id,
            service_arrangement_data=service_arrangement_data,
            therapist_id=selected_therapist_id,
            therapist_data=therapist_data,
            appointment_start=appointment_start,
            appointment_end=appointment_end,
            duration_minutes=service_base_duration,
            extra_minutes=int(body.extra_minutes or 0),
            total_duration=body.total_duration,
            addons_duration=int(body.addons_duration or 0) if body.addons_duration is not None else None,
            base_price=Decimal(str(body.base_price)) if body.base_price is not None else None,
            pricing=pricing,
            addons=addon_records,
            customer_notes=body.customer_notes or body.customer_message or None,
            payment_data=body.payment_data,
            idempotency_key=idempotency_key,
            booking_type=booking_type_str,
            payment_type=payment_type_str,
            loyalty_data=body.loyalty_data,
            reward_id=body.reward_id,
            voucher_id=body.voucher_id,
            voucher_data=body.voucher_data,
            created_by=created_by,
        )
    except DoubleBookingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
        )
    except USHBaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )

    # ── 8. Return success response ──────────────────────────────────────
    return CreateBookingResponse(
        success=True,
        data=CreateBookingDataResponse(
            booking_id=str(booking.id),
            customer_id=str(booking.customer_id),
            final_amount=str(booking.total_amount),
            status=booking.status,
            payment_status=booking.payment_status,
            is_eligible_for_loyalty=bool((booking.service_data or {}).get("is_eligible_for_loyalty", False)),
            loyalty_data=getattr(booking, "loyalty_data", None),
            reward_id=str(booking.reward_id) if getattr(booking, "reward_id", None) else None,
            voucher_id=str(booking.voucher_id) if getattr(booking, "voucher_id", None) else None,
            voucher_data=getattr(booking, "voucher_data", None),
        ),
    )


@router.get(
    "/",
    response_model=None,
    summary="List all bookings (public with filters)",
    description=(
        "Public endpoint to retrieve and filter bookings by status, payment_status, date, "
        "customer, branch, therapist, service, etc. Supports pagination."
    ),
)
async def list_bookings(
    booking_service: BookingServiceDep,
    request: Request,
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    status: str | None = Query(default=None, description="Filter by booking status: requested, confirmed, cancelled, etc."),
    payment_status: str | None = Query(default=None, description="Filter by payment status: pending, success, failed, etc."),
    date: str | None = Query(default=None, description="Filter by appointment date (YYYY-MM-DD)"),
    from_date: str | None = Query(default=None, description="Filter bookings on or after date (YYYY-MM-DD)"),
    to_date: str | None = Query(default=None, description="Filter bookings on or before date (YYYY-MM-DD)"),
    customer_id: uuid.UUID | None = Query(default=None, description="Filter by customer ID"),
    branch_id: uuid.UUID | None = Query(default=None, description="Filter by branch ID"),
    therapist_id: uuid.UUID | None = Query(default=None, description="Filter by therapist ID"),
    service_id: uuid.UUID | None = Query(default=None, description="Filter by service ID"),
    service_arrangement_id: uuid.UUID | None = Query(default=None, description="Filter by service arrangement ID"),
    search: str | None = Query(default=None, description="Search across customer details, notes, etc."),
) -> JSONResponse:
    """List bookings publicly with filtering and pagination."""
    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 20)

    bookings, total = await booking_service.list_bookings(
        page=p_page,
        page_size=p_page_size,
        status=_val(status),
        payment_status=_val(payment_status),
        date_str=_val(date),
        from_date=_val(from_date),
        to_date=_val(to_date),
        customer_id=_val(customer_id),
        branch_id=_val(branch_id),
        therapist_id=_val(therapist_id),
        service_id=_val(service_id),
        service_arrangement_id=_val(service_arrangement_id),
        search=_val(search),
    )

    items = [_booking_to_list_item(b) for b in bookings]
    paginated = make_paginated_response(
        items,
        count=total,
        page=p_page,
        page_size=p_page_size,
        base_url=str(request.url.remove_query_params(["page", "page_size"])),
    )

    return JSONResponse(content=paginated.model_dump(mode="json"))


@router.get(
    "/my-bookings/",
    response_model=None,
    summary="List my bookings (private)",
    description="Private endpoint to list bookings for the authenticated user only. Requires user token.",
)
async def list_my_bookings(
    current_user: CurrentUser,
    booking_service: BookingServiceDep,
    request: Request,
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    status: str | None = Query(default=None, description="Filter by booking status"),
    payment_status: str | None = Query(default=None, description="Filter by payment status"),
    date: str | None = Query(default=None, description="Filter by appointment date (YYYY-MM-DD)"),
    from_date: str | None = Query(default=None, description="Filter bookings on or after date"),
    to_date: str | None = Query(default=None, description="Filter bookings on or before date"),
) -> JSONResponse:
    """List all bookings for the authenticated customer."""
    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 20)
    customer_id = uuid.UUID(current_user.sub)

    bookings, total = await booking_service.list_customer_bookings(
        customer_id=customer_id,
        page=p_page,
        page_size=p_page_size,
        status_filter=_val(status),
        payment_status_filter=_val(payment_status),
        date_str=_val(date),
        from_date=_val(from_date),
        to_date=_val(to_date),
    )

    items = [_booking_to_list_item(b) for b in bookings]
    paginated = make_paginated_response(
        items,
        count=total,
        page=p_page,
        page_size=p_page_size,
        base_url=str(request.url.remove_query_params(["page", "page_size"])),
    )

    return JSONResponse(content=paginated.model_dump(mode="json"))


@router.get(
    "/{booking_id}/",
    summary="Get booking detail",
    response_model=None,
)
async def get_booking(
    booking_id: uuid.UUID,
    _: RequireAppToken,
    booking_service: BookingServiceDep,
) -> JSONResponse:
    """Get full detail of a booking. Requires only the USHSPA app token (public)."""
    booking = await booking_service.get_booking(booking_id, load_history=True)

    return JSONResponse(
        content={"success": True, "data": _booking_to_detail(booking).model_dump(mode="json")}
    )


@router.patch(
    "/{booking_id}/",
    summary="Update booking (partial)",
    description=(
        "Update booking status, payment status, timing, therapist, or notes. "
        "Follows REST standards for resource updates."
    ),
    response_model=UpdateBookingResponse,
)
@router.put(
    "/{booking_id}/",
    summary="Update booking (full/partial)",
    description="Update booking details using REST PUT.",
    response_model=UpdateBookingResponse,
)
async def update_booking(
    booking_id: uuid.UUID,
    body: UpdateBookingRequest,
    current_user: CurrentUser,
    booking_service: BookingServiceDep,
    ushauth: USHAuthDep,
) -> JSONResponse:
    """Update a booking resource."""
    booking = await booking_service.get_booking(booking_id)

    # Check permission if called by non-admin customer
    user_id = uuid.UUID(current_user.sub)
    if body.source == "customer" and booking.customer_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You can only update your own bookings.",
        )

    therapist_data = None
    if body.therapist_id:
        try:
            t_raw = await ushauth.get_therapist(str(body.therapist_id))
            therapist_data = t_raw.get("data") if isinstance(t_raw.get("data"), dict) else t_raw
        except Exception:
            pass

    updated = await booking_service.update_booking(
        booking_id=booking_id,
        status=body.status,
        payment_status=body.payment_status,
        payment_data=body.payment_data,
        therapist_id=body.therapist_id,
        therapist_data=therapist_data,
        appointment_start=body.appointment_start,
        extra_minutes=body.extra_minutes,
        customer_notes=body.customer_notes,
        internal_notes=body.internal_notes,
        reason=body.reason,
        source=body.source,
        changed_by=str(user_id),
        voucher_id=body.voucher_id,
        voucher_data=body.voucher_data,
    )

    return JSONResponse(
        content={
            "success": True,
            "data": _booking_to_detail(updated).model_dump(mode="json"),
        }
    )


@router.post(
    "/{booking_id}/cancel/",
    summary="Cancel a booking",
    response_model=None,
)
async def cancel_booking(
    booking_id: uuid.UUID,
    body: CancelBookingRequest,
    current_user: CurrentUser,
    booking_service: BookingServiceDep,
) -> JSONResponse:
    """Cancel a booking. Only the booking owner can cancel."""
    booking = await booking_service.get_booking(booking_id)

    if booking.customer_id != uuid.UUID(current_user.sub):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    updated = await booking_service.cancel_booking(
        booking_id,
        reason=body.reason,
        cancelled_by="customer",
        correlation_id=None,
    )

    return JSONResponse(
        content={
            "success": True,
            "data": {"booking_id": str(updated.id), "status": updated.status},
        }
    )


@router.post(
    "/{booking_id}/reschedule/",
    summary="Request reschedule",
    response_model=None,
)
async def request_reschedule(
    booking_id: uuid.UUID,
    body: RescheduleRequest,
    current_user: CurrentUser,
    booking_service: BookingServiceDep,
) -> JSONResponse:
    """
    Request a booking reschedule.

    Requires at least 6 hours notice. Admin will approve/deny manually.
    """
    customer_id = uuid.UUID(current_user.sub)

    booking = await booking_service.get_booking(booking_id)
    duration = booking.duration_minutes
    new_end = body.new_start + timedelta(minutes=duration)

    updated = await booking_service.request_reschedule(
        booking_id,
        new_start=body.new_start,
        new_end=new_end,
        customer_id=customer_id,
    )

    return JSONResponse(
        content={
            "success": True,
            "data": {
                "booking_id": str(updated.id),
                "status": updated.status,
                "message": "Reschedule request submitted. Our team will review it shortly.",
            },
        }
    )


@router.patch(
    "/{booking_id}/status/",
    summary="Update booking status",
    description=(
        "Update the status of a booking (requested, confirmed, in_progress, completed, cancelled, no_show). "
        "Records an audit trail in status history and publishes an SQS event to AWS_SQS_NOTIFICATION_QUEUE_URL. "
        "Requires USHSPA-TOKEN."
    ),
)
@router.post(
    "/{booking_id}/status/",
    summary="Update booking status",
    include_in_schema=False,
)
async def update_booking_status(
    booking_id: uuid.UUID,
    body: UpdateBookingStatusRequest,
    _: RequireAppToken,
    booking_service: BookingServiceDep,
    ushauth: USHAuthDep,
) -> JSONResponse:
    """Update booking status and dispatch SQS event."""
    payment_status = None
    if body.payment_status:
        if isinstance(body.payment_status, str):
            try:
                payment_status = PaymentStatus(body.payment_status.lower())
            except ValueError:
                payment_status = None
        else:
            payment_status = body.payment_status
    elif body.payment_data and (
        body.payment_data.get("is_paid") is True
        or str(body.payment_data.get("status", "")).lower() in ("paid", "success")
    ):
        payment_status = PaymentStatus.SUCCESS

    updated = await booking_service.update_status(
        booking_id=booking_id,
        new_status=body.status,
        payment_status=payment_status,
        payment_data=body.payment_data,
        reason=body.reason,
        source=body.source,
        loyalty_data=body.loyalty_data,
        reward_id=body.reward_id,
        voucher_id=body.voucher_id,
        voucher_data=body.voucher_data,
    )

    return JSONResponse(
        content={
            "success": True,
            "data": _booking_to_detail(updated).model_dump(mode="json"),
        }
    )
