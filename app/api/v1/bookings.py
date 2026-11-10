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

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update

from app.api.deps import AppSettings, BookingServiceDep, CurrentUser, DBSession, OptionalUser, RequireAppToken, USHAuthDep
from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
from app.payment.infrastructure.models import Payment, PaymentStatusHistory
from app.events.contracts import BookingPaymentStatusSuccessEvent, BookingUpdatedEvent
from app.booking.application.multi_service import ServiceValidationError, resolve_service_entries
from app.booking.application.session_planner import (
    SessionAvailabilityError,
    SessionPlan,
    plan_sessions,
    totals_as_decimals,
)
from app.booking.domain.sessions import SessionValidationError
from app.booking.application.multi_therapist import (
    entries_required_therapists,
    parse_uuid,
    therapist_display_name,
    unique_ids,
)
from app.booking.application.services import _build_booking_event_data
from app.booking.domain.services_snapshot import (
    cache_services_payload,
    distinct_arrangement_ids,
    get_booking_services,
    joined_service_name,
    points_cost,
    total_services_duration,
    total_services_price,
)
from app.booking.domain.sessions import (
    cache_sessions_payload,
    get_booking_sessions,
    session_window,
)
from app.booking.interfaces.schemas import (
    AddonSchema,
    BookingDetailResponse,
    BookingListItem,
    BookingListResponse,
    BookingServiceResponse,
    BookingSessionResponse,
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
from app.common.utils import local_now, to_local_tz
from app.core.exceptions import (
    AuthorizationError,
    BookingNotFoundError,
    DoubleBookingError,
    GatewayError,
    GatewayTimeoutError,
    NotFoundError,
    USHBaseError,
    ValidationError as CoreValidationError,
)
from app.core.logging import get_logger
from app.course.domain.value_objects import (
    COURSE_BOOKING_TYPE,
    COURSE_EXTRAS_NOT_ALLOWED,
    COURSE_PAYMENT_METHOD,
    COURSE_PAYMENT_PROVIDER,
    COURSE_PAYMENT_TYPE,
    CourseError,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/bookings", tags=["Bookings"])


def _safe_str(val: Any) -> str | None:
    """Safely extract string, ignoring non-string/non-scalar types like MagicMock."""
    if val is None or not isinstance(val, (str, int, float, uuid.UUID)):
        return None
    s = str(val).strip()
    return s if s else None


def _services_out(b: object) -> list[BookingServiceResponse]:
    """Map the booking's service entries to response items (legacy rows → 1 item)."""
    out: list[BookingServiceResponse] = []
    for idx, e in enumerate(get_booking_services(b)):
        out.append(
            BookingServiceResponse(
                service_id=str(e.get("service_id") or ""),
                service_data=e.get("service_data") if isinstance(e.get("service_data"), dict) else {},
                service_arrangement_id=str(e["service_arrangement_id"]) if e.get("service_arrangement_id") else None,
                service_arrangement_data=e.get("service_arrangement_data") if isinstance(e.get("service_arrangement_data"), dict) else {},
                base_price=str(e["base_price"]) if e.get("base_price") is not None else None,
                duration_minutes=int(e.get("duration_minutes") or 0),
                position=idx,
                session_id=str(e["session_id"]) if e.get("session_id") else None,
            )
        )
    return out


def _sessions_out(b: object) -> list[BookingSessionResponse]:
    """Map the booking's sessions to response items (legacy rows → 1 synthesized session)."""
    try:
        out: list[BookingSessionResponse] = []
        for idx, s in enumerate(get_booking_sessions(b)):
            payload = dict(s)
            payload["session_id"] = str(payload.get("session_id") or "")
            payload["position"] = idx
            payload["service_ids"] = [str(x) for x in (payload.get("service_ids") or [])]
            if not payload.get("service_name"):
                names = [
                    str(
                        (e.get("service_data") or {}).get("name")
                        or (e.get("service_data") or {}).get("service_name")
                        or e.get("name")
                        or e.get("service_name")
                        or ""
                    )
                    for e in payload.get("services") or []
                ]
                payload["service_name"] = " + ".join(n for n in names if n)
            out.append(BookingSessionResponse(**payload))
        return out
    except Exception as exc:  # never break a booking response because of sessions
        logger.warning("sessions_out_failed", error=str(exc))
        return []


def _session_count(b: object) -> int:
    try:
        return max(len(get_booking_sessions(b)), 1)
    except Exception:
        return 1


def _secondary_therapist_id_out(b: object) -> str | None:
    """Second therapist ID of a couple/double booking (None for every other booking)."""
    raw = getattr(b, "secondary_therapist_id", None)
    return str(raw) if isinstance(raw, (str, uuid.UUID)) and raw else None


def _secondary_therapist_data_out(b: object) -> dict | None:
    raw = getattr(b, "secondary_therapist_data", None)
    return raw if isinstance(raw, dict) else None


def _therapist_ids_out(b: object) -> list[str]:
    """All assigned therapist IDs: primary first, then the secondary (if any)."""
    ids = [str(b.therapist_id)]
    secondary = _secondary_therapist_id_out(b)
    if secondary:
        ids.append(secondary)
    return ids


def _is_therapist_user(user: Any) -> bool:
    """True when the authenticated employee holds the (non-admin) Therapist role."""
    if user is None or getattr(user, "is_superuser", None):
        return False
    perms = getattr(user, "permissions", None)
    if isinstance(perms, dict) and perms.get("is_therapist_role") is True:
        return True
    return str(getattr(user, "role", "") or "").strip().lower() == "therapist"


def _therapist_key(user: Any) -> str:
    """Therapist profile id used on bookings (falls back to the user id if ushauth did not send it)."""
    perms = getattr(user, "permissions", None)
    tid = perms.get("therapist_id") if isinstance(perms, dict) else None
    return str(tid or user.sub)


def _booking_involves_therapist(b: object, therapist_id: str) -> bool:
    """True when ``therapist_id`` is the primary/secondary or a session therapist of the booking."""
    tid = str(therapist_id).lower()
    if str(getattr(b, "therapist_id", "") or "").lower() == tid:
        return True
    if (_secondary_therapist_id_out(b) or "").lower() == tid:
        return True
    return tid in str(getattr(b, "sessions", "") or "").lower()


def _deny_therapist(user: Any) -> None:
    """Therapists are read-only (plus → completed); block every other mutation."""
    if _is_therapist_user(user):
        raise HTTPException(
            status_code=403,
            detail={"code": "PERMISSION_DENIED", "message": "Therapists cannot modify bookings."},
        )


def _therapist_window_dates() -> tuple[str, str]:
    """(today, today+7) in the spa timezone — the only range a therapist may query."""
    today = local_now().date()
    return today.isoformat(), (today + timedelta(days=7)).isoformat()


def _course_subscription_id_out(b: object) -> str | None:
    raw = getattr(b, "course_subscription_id", None)
    return str(raw) if isinstance(raw, (str, uuid.UUID)) and raw else None


def _course_data_out(b: object) -> dict | None:
    raw = getattr(b, "course_data", None)
    return raw if isinstance(raw, dict) else None


def _course_http_error(exc: CourseError) -> HTTPException:
    """Keep the course error code in the standard error envelope (HTTPException handler)."""
    http_exc = HTTPException(status_code=int(exc.status_code), detail=exc.message)
    http_exc.code = exc.code  # type: ignore[attr-defined]
    return http_exc


def _booking_to_list_item(b: object) -> BookingListItem:
    """Map ORM Booking to BookingListItem."""
    b_type = getattr(b, "booking_type", "branch")
    booking_type_val = b_type if isinstance(b_type, str) else "branch_service"
    raw_base_price = getattr(b, "base_price", None)
    raw_app_date = getattr(b, "appointment_date", None) or getattr(b, "appointment_start", None)
    return BookingListItem(
        id=str(b.id),
        booking_number=_safe_str(getattr(b, "booking_number", None)),
        customer_id=str(b.customer_id),
        customer_data=b.customer_data,
        branch_id=str(b.branch_id) if b.branch_id else None,
        branch_data=b.branch_data,
        service_id=str(b.service_id),
        service_data=b.service_data,
        service_arrangement_id=str(b.service_arrangement_id) if b.service_arrangement_id else None,
        service_arrangement_data=b.service_arrangement_data,
        services=_services_out(b),
        sessions=_sessions_out(b),
        session_count=_session_count(b),
        therapist_id=str(b.therapist_id),
        therapist_data=b.therapist_data,
        secondary_therapist_id=_secondary_therapist_id_out(b),
        secondary_therapist_data=_secondary_therapist_data_out(b),
        therapist_ids=_therapist_ids_out(b),
        appointment_date=raw_app_date,
        appointment_start=b.appointment_start,
        appointment_end=b.appointment_end,
        duration_minutes=int(getattr(b, "duration_minutes", 0) or 0),
        extra_minutes=int(getattr(b, "extra_minutes", 0) or 0),
        price_for_extra_minutes=_safe_str(getattr(b, "price_for_extra_minutes", None)),
        customer_notes=_safe_str(getattr(b, "customer_notes", None)),
        total_duration=getattr(b, "total_duration", None),
        addons_duration=getattr(b, "addons_duration", None),
        base_price=str(raw_base_price) if raw_base_price is not None else None,
        booking_type=booking_type_val,
        payment_type=getattr(b, "payment_type", "service") or "service",
        status=b.status,
        payment_status=b.payment_status,
        payment_id=_safe_str(getattr(b, "payment_id", None)),
        payment_provider=_safe_str(getattr(b, "payment_provider", None)),
        payment_gateway=_safe_str(getattr(b, "payment_gateway", None)),
        payment_through=_safe_str(getattr(b, "payment_through", None)),
        payment_method=_safe_str(getattr(b, "payment_method", None)),
        payment_url=_safe_str(getattr(b, "payment_url", None)),
        payment_data=b.payment_data or {},
        total_amount=str(b.total_amount),
        total_price=str(b.total_amount),
        discount=str(b.discount),
        arrangement_price=str(b.arrangement_price),
        addon_price=str(b.addon_price),
        currency=b.currency,
        is_eligible_for_loyalty=bool((b.service_data or {}).get("is_eligible_for_loyalty", False)),
        loyalty_data=getattr(b, "loyalty_data", None) if isinstance(getattr(b, "loyalty_data", None), dict) else None,
        reward_id=str(b.reward_id) if getattr(b, "reward_id", None) and isinstance(getattr(b, "reward_id", None), (str, uuid.UUID)) else None,
        voucher_id=str(b.voucher_id) if getattr(b, "voucher_id", None) and isinstance(getattr(b, "voucher_id", None), (str, uuid.UUID)) else None,
        voucher_data=getattr(b, "voucher_data", None) if isinstance(getattr(b, "voucher_data", None), dict) else None,
        course_subscription_id=_course_subscription_id_out(b),
        course_data=_course_data_out(b),
        created_at=b.created_at,
        created_by_user=_safe_str(getattr(b, "created_by_user", None) or getattr(b, "created_by", None)),
        created_by_user_data=getattr(b, "created_by_user_data", None) if isinstance(getattr(b, "created_by_user_data", None), dict) else None,
        created_by=_safe_str(getattr(b, "created_by_user", None) or getattr(b, "created_by", None)),
        invoice_number=_safe_str(getattr(b, "invoice_number", None)),
        payment_number=_safe_str(getattr(b, "payment_number", None) or (b.payment_data or {}).get("payment_number")),
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
                change_by_user=getattr(h, "change_by_user", None),
                change_by_user_data=getattr(h, "change_by_user_data", None),
                reason=h.reason,
                created_at=h.created_at,
            )
            for h in sorted(raw_history, key=lambda x: x.created_at)
        ]

    # Always include is_eligible_for_loyalty inside service_data so the
    # client receives a consistent shape regardless of when the booking was created.
    _raw_service_data = b.service_data or {}
    is_eligible = bool(_raw_service_data.get("is_eligible_for_loyalty", False))
    service_data_out = {
        **_raw_service_data,
        "is_eligible_for_loyalty": is_eligible,
        "loyalty_points": int(_raw_service_data.get("loyalty_points") or 0),
        "price_in_points": int(_raw_service_data.get("price_in_points") or 0),
    }


    raw_base_price = getattr(b, "base_price", None)
    raw_app_date = getattr(b, "appointment_date", None) or getattr(b, "appointment_start", None)
    raw_branch_data = b.branch_data if isinstance(getattr(b, "branch_data", None), dict) else None
    raw_arr_data = b.service_arrangement_data if isinstance(getattr(b, "service_arrangement_data", None), dict) else None
    return BookingDetailResponse(
        id=str(b.id),
        booking_number=_safe_str(getattr(b, "booking_number", None)),
        customer_id=str(b.customer_id),
        customer_data=b.customer_data if isinstance(getattr(b, "customer_data", None), dict) else {},
        branch_id=str(b.branch_id) if getattr(b, "branch_id", None) and isinstance(b.branch_id, (str, uuid.UUID)) else None,
        branch_data=raw_branch_data,
        service_id=str(b.service_id),
        service_data=service_data_out,
        service_arrangement_id=str(b.service_arrangement_id) if getattr(b, "service_arrangement_id", None) and isinstance(b.service_arrangement_id, (str, uuid.UUID)) else None,
        service_arrangement_data=raw_arr_data,
        services=_services_out(b),
        sessions=_sessions_out(b),
        session_count=_session_count(b),
        therapist_id=str(b.therapist_id),
        therapist_data=b.therapist_data if isinstance(getattr(b, "therapist_data", None), dict) else {},
        secondary_therapist_id=_secondary_therapist_id_out(b),
        secondary_therapist_data=_secondary_therapist_data_out(b),
        therapist_ids=_therapist_ids_out(b),
        appointment_date=raw_app_date,
        appointment_start=b.appointment_start,
        appointment_end=b.appointment_end,
        duration_minutes=int(getattr(b, "duration_minutes", 0) or 0),
        extra_minutes=int(getattr(b, "extra_minutes", 0) or 0),
        total_duration=getattr(b, "total_duration", None),
        addons_duration=getattr(b, "addons_duration", None),
        base_price=str(raw_base_price) if raw_base_price is not None else None,
        price_for_extra_minutes=str(b.price_for_extra_minutes),
        booking_type=_safe_str(getattr(b, "booking_type", None)) or "branch_service",
        payment_type=_safe_str(getattr(b, "payment_type", None)) or "service",
        status=b.status,
        payment_status=b.payment_status,
        payment_id=_safe_str(getattr(b, "payment_id", None)),
        payment_provider=_safe_str(getattr(b, "payment_provider", None)),
        payment_gateway=_safe_str(getattr(b, "payment_gateway", None)),
        payment_through=_safe_str(getattr(b, "payment_through", None)),
        payment_method=_safe_str(getattr(b, "payment_method", None)),
        payment_url=_safe_str(getattr(b, "payment_url", None)),
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
        total_amount=str(b.total_amount),
        total_price=str(b.total_amount),
        discount=str(b.discount),
        arrangement_price=str(b.arrangement_price),
        addon_price=str(b.addon_price),
        currency=b.currency,
        addons=addons,
        customer_notes=b.customer_notes,
        internal_notes=b.internal_notes,
        status_history=history,
        is_eligible_for_loyalty=is_eligible,
        loyalty_data=getattr(b, "loyalty_data", None) if isinstance(getattr(b, "loyalty_data", None), dict) else None,
        reward_id=str(b.reward_id) if getattr(b, "reward_id", None) and isinstance(getattr(b, "reward_id", None), (str, uuid.UUID)) else None,
        voucher_id=str(b.voucher_id) if getattr(b, "voucher_id", None) and isinstance(getattr(b, "voucher_id", None), (str, uuid.UUID)) else None,
        voucher_data=getattr(b, "voucher_data", None) if isinstance(getattr(b, "voucher_data", None), dict) else None,
        course_subscription_id=_course_subscription_id_out(b),
        course_data=_course_data_out(b),
        created_at=b.created_at,
        updated_at=b.updated_at,
        created_by_user=_safe_str(getattr(b, "created_by_user", None) or getattr(b, "created_by", None)),
        created_by_user_data=getattr(b, "created_by_user_data", None) if isinstance(getattr(b, "created_by_user_data", None), dict) else None,
        created_by=_safe_str(getattr(b, "created_by_user", None) or getattr(b, "created_by", None)),
        invoice_number=_safe_str(getattr(b, "invoice_number", None) or (b.payment_data or {}).get("invoice_number")),
        invoice_id=_safe_str(getattr(b, "invoice_id", None) or (b.payment_data or {}).get("invoice_id")),
        payment_number=_safe_str(getattr(b, "payment_number", None) or (b.payment_data or {}).get("payment_number")),
        refund_number=_safe_str(getattr(b, "refund_number", None)),
        refund_id=_safe_str(getattr(b, "refund_id", None)),
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
    # ── Always track who made the request (ushdesk staff or ushspa customer) ─
    req_user_id: str = str(
        body.change_by_user
        or body.created_by_user
        or body.created_by
        or current_user.sub
    )
    user_type_val = _safe_str(getattr(current_user, "user_type", None)) or "customer"
    role_val = _safe_str(getattr(current_user, "role", None))
    req_user_data = body.change_by_user_data or body.created_by_user_data
    if req_user_data is None:
        req_user_data = {
            "id": current_user.sub,
            "first_name": current_user.first_name or "",
            "last_name": current_user.last_name or "",
            "phone_number": current_user.phone_number or "",
            "email": current_user.email or "",
            "user_type": user_type_val,
            "role": role_val,
        }
    else:
        req_user_data = dict(req_user_data)
        if "user_type" not in req_user_data:
            req_user_data["user_type"] = user_type_val
        if "role" not in req_user_data:
            req_user_data["role"] = role_val
    created_by_user: str = req_user_id
    created_by: str = req_user_id
    created_by_user_data: dict[str, Any] = req_user_data

    # Resolve source: ushdesk, ushspa, or payment_through
    request_source = (
        body.payment_through
        or request.headers.get("x-client-app")
        or request.headers.get("x-app-source")
    )
    if not request_source:
        user_role = str(getattr(current_user, "role", "") or "").lower()
        if user_role in ("admin", "agent", "staff", "desk"):
            request_source = "ushdesk"
        else:
            request_source = "ushspa"

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
            _cust_lang_raw = (
                cust_profile.get("language_preference")
                or cust_user.get("language_preference")
                or cust_profile.get("language")
                or cust_user.get("language")
                or "en"
            )
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
                "language_preference": "ar" if str(_cust_lang_raw).strip().lower().startswith("ar") else "en",
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
                "language_preference": getattr(current_user, "language_preference", None) or "en",
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
            "language_preference": getattr(current_user, "language_preference", None) or "en",
        }

    # ── 1. Resolve date, time, duration & therapist IDs ─────────────────
    # appointment_date / appointment_time are now declared fields in the schema
    # with validators that sanitize milliseconds and timezone suffixes.
    # Fall back to legacy date / time_slot / appointment_start fields.
    appointment_date: str = (
        body.appointment_date
        or body.date
        or (to_local_tz(body.appointment_start).strftime("%Y-%m-%d") if body.appointment_start else "")
    )
    # Strip any residual time/tz suffix (safety net)
    appointment_date = str(appointment_date).split("T")[0].strip()

    raw_time: str = (
        body.appointment_time
        or body.start_time
        or body.time_slot
        or (to_local_tz(body.appointment_start).strftime("%H:%M:%S") if body.appointment_start else "")
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

    booking_type_str = (
        body.booking_type.value
        if hasattr(body.booking_type, "value")
        else str(body.booking_type or "branch_service")
    )
    # The bookings.booking_type CHECK constraint only allows the canonical values;
    # map legacy aliases ("home" / "branch") sent by older clients.
    booking_type_str = {"home": "home_service", "branch": "branch_service"}.get(
        booking_type_str.strip().lower(), booking_type_str.strip().lower()
    ) or "branch_service"

    # ── Detect loyalty redemption booking ─────────────────────────────
    is_loyalty_booking = (
        booking_type_str == "loyalty"
        or (body.payment_type and str(body.payment_type).lower() == "rewarded")
        or body.reward_id is not None
        or bool(body.loyalty_data and (body.loyalty_data.get("points_cost") or body.loyalty_data.get("reward_id")))
    )

    # Auto-derive payment_type from booking_type when not explicitly provided
    if body.payment_type:
        payment_type_str = body.payment_type
    elif is_loyalty_booking or booking_type_str == "loyalty":
        payment_type_str = "rewarded"
    elif booking_type_str == "gift_voucher":
        payment_type_str = "gift_voucher"
    else:
        payment_type_str = "service"

    # ── Course redemption (contract E): the server forces the course semantics ──
    is_course_redemption = getattr(body, "course_subscription_id", None) is not None
    if is_course_redemption:
        if not current_user.has_permission("course_subscriptions.redeem"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PERMISSION_DENIED",
                    "message": "Permission 'course_subscriptions.redeem' is required.",
                    "required": "course_subscriptions.redeem",
                },
            )
        if body.reward_id is not None or body.voucher_id is not None or body.loyalty_data:
            raise _course_http_error(
                CourseError(COURSE_EXTRAS_NOT_ALLOWED, "A course session cannot be combined with rewards or vouchers.")
            )
        booking_type_str = COURSE_BOOKING_TYPE
        payment_type_str = COURSE_PAYMENT_TYPE
        is_loyalty_booking = False

    if not is_course_redemption and (is_loyalty_booking or booking_type_str == "loyalty" or body.loyalty_data is not None):
        if booking_type_str in ("branch_service", "branch", ""):
            booking_type_str = "loyalty"
        request_source = "ushspa"
        if not body.payment_through:
            body.payment_through = "ushspa"

    # ── Resolve services: multi-session path or legacy flat path ─────────
    _session_plan: SessionPlan | None = None
    _svcs_price: Decimal | None = None  # set by multi-session pricing branch
    fallback_base: Decimal | None = None  # set by legacy pricing branch

    if body.sessions:
        # ── NEW: multi-session booking ───────────────────────────────────
        try:
            _session_plan = await plan_sessions(
                ushauth,
                body.sessions,
                appointment_date=appointment_date,
                is_loyalty_booking=bool(is_loyalty_booking),
                check_availability=True,
                branch_id=str(body.branch_id) if body.branch_id else None,
                is_home_service=(booking_type_str in ("home_service", "home")),
            )
        except SessionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        except SessionAvailabilityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
        service_entries = _session_plan.service_entries
    else:
        # ── LEGACY: flat multi-service booking ───────────────────────────
        try:
            service_entries = await resolve_service_entries(
                ushauth,
                body.services or [],
                is_loyalty_booking=bool(is_loyalty_booking),
            )
        except ServiceValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )

    if _session_plan is not None:
        # Multi-session: totals come from the plan
        duration = int(_session_plan.totals["total_duration"])
    elif len(service_entries) > 1:
        # Never reserve less than the sum of the booked services' durations.
        duration = max(int(body.total_duration or 0), total_services_duration(service_entries)) or 60
    else:
        duration = int(body.total_duration or body.base_duration or 60)
    service_name = joined_service_name(service_entries)
    entries_points_cost = sum(points_cost(e) for e in service_entries)

    # ── Status & Payment Status Resolution ────────────────────────────
    status_str: str | None = None
    if body.status:
        raw_status = (
            body.status.value
            if hasattr(body.status, "value")
            else str(body.status)
        ).strip().lower()
        if raw_status in BookingStatus._value2member_map_:
            status_str = raw_status

    pstatus_str: str | None = None
    if body.payment_status:
        raw_pstatus = (
            body.payment_status.value
            if hasattr(body.payment_status, "value")
            else str(body.payment_status)
        ).strip().lower()
        if raw_pstatus in ("paid", "success"):
            pstatus_str = PaymentStatus.SUCCESS.value
        elif raw_pstatus in PaymentStatus._value2member_map_:
            pstatus_str = raw_pstatus

    # Auto-derive payment_status from payment_data if not explicitly supplied
    if not pstatus_str and body.payment_data:
        if (
            body.payment_data.get("is_paid") is True
            or str(body.payment_data.get("status", "")).lower() in ("paid", "success")
        ):
            pstatus_str = PaymentStatus.SUCCESS.value

    # Auto-infer relationships between status and payment_status
    if is_loyalty_booking or booking_type_str == "loyalty" or payment_type_str == "rewarded":
        if not pstatus_str:
            pstatus_str = PaymentStatus.REWARDED.value
        if not status_str:
            status_str = BookingStatus.CONFIRMED.value
    elif status_str == BookingStatus.CONFIRMED.value:
        if not pstatus_str:
            pstatus_str = PaymentStatus.SUCCESS.value
    elif pstatus_str == PaymentStatus.SUCCESS.value:
        if not status_str:
            status_str = BookingStatus.CONFIRMED.value

    final_status = status_str or BookingStatus.REQUESTED.value
    final_payment_status = pstatus_str or PaymentStatus.NOT_INITIATED.value
    if is_course_redemption:
        final_status = BookingStatus.CONFIRMED.value
        final_payment_status = PaymentStatus.SUCCESS.value

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

    # Couple / double rooms: two guests, same service, same time → two therapists.
    # The primary stays first so single-therapist clients are unaffected.
    required_therapists = entries_required_therapists(service_entries)
    secondary_requested_id = str(body.secondary_therapist_id) if body.secondary_therapist_id else None
    allow_single_therapist = bool(getattr(body, "allow_single_therapist", False))
    if allow_single_therapist and not secondary_requested_id:
        # User explicitly removed the second therapist: book with one therapist only.
        required_therapists = 1
    if required_therapists > 1 or secondary_requested_id:
        therapist_ids = unique_ids([therapist_id_str, secondary_requested_id, *therapist_ids])
        if not therapist_id_str and therapist_ids:
            therapist_id_str = therapist_ids[0]
    selected_secondary_therapist_id: uuid.UUID | None = None

    # ── 2. Check appointment availability in ushauth microservice ───────
    # For multi-session bookings availability is already checked per-session
    # inside plan_sessions(). For legacy flat bookings we check here as usual.
    selected_therapist_id: uuid.UUID | None = None

    if _session_plan is not None:
        # Multi-session: derive therapist info from session 0 for legacy fields
        _first_s = _session_plan.sessions[0]
        _ts0_str = _first_s.get("therapist_id")
        selected_therapist_id = uuid.UUID(_ts0_str) if _ts0_str else None
        _sec0_str = _first_s.get("secondary_therapist_id")
        selected_secondary_therapist_id = uuid.UUID(_sec0_str) if _sec0_str else None
        required_therapists = 1
        service_arrangement_id = _first_s.get("service_arrangement_id") or service_arrangement_id
    else:
        if booking_type_str in ("branch_service", "branch", "loyalty", COURSE_BOOKING_TYPE) and service_arrangement_id:
            # ── Branch booking: arrangement-level availability check ─────────
            if not therapist_id_str and not therapist_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="At least one therapist ID must be provided to check appointment availability.",
                )
            try:
                avail_res = await ushauth.check_appointment_availability(
                    service_arrangement_id=service_arrangement_id,
                    appointment_date=appointment_date,
                    appointment_time=appointment_time,
                    duration=duration,
                    therapist_id=therapist_id_str,
                    therapist_ids=therapist_ids if therapist_ids else None,
                    required_therapists=required_therapists,
                    allow_single_therapist=allow_single_therapist,
                    branch_id=str(body.branch_id) if body.branch_id else None,
                    service_id=str(service_entries[0].get("service_id")) if service_entries else None,
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

            # ushauth is authoritative about how many therapists the arrangement needs
            required_therapists = max(required_therapists, int(avail_res.get("required_therapists") or 1))
            if required_therapists > 1:
                available_ids = unique_ids(
                    [selected_therapist_id_str, *(avail_res.get("therapist_ids") or []), avail_res.get("secondary_therapist_id")]
                )
                selected_secondary_therapist_id = parse_uuid(available_ids[1]) if len(available_ids) > 1 else None
                if not selected_secondary_therapist_id or selected_secondary_therapist_id == selected_therapist_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Two available therapists are required for a couple/double room at the requested time slot.",
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

    # ── 2b. Multi-service: remaining arrangements, same therapist & window ─
    if _session_plan is None and booking_type_str in ("branch_service", "branch", "loyalty", COURSE_BOOKING_TYPE) and selected_therapist_id:
        extra_arrangement_ids = [
            a for a in distinct_arrangement_ids(service_entries) if a != service_arrangement_id
        ]
        if extra_arrangement_ids:
            extra_results = await asyncio.gather(
                *(
                    ushauth.check_appointment_availability(
                        service_arrangement_id=arr_id,
                        appointment_date=appointment_date,
                        appointment_time=appointment_time,
                        duration=duration,
                        therapist_id=str(selected_therapist_id),
                        therapist_ids=(
                            [str(selected_therapist_id), str(selected_secondary_therapist_id)]
                            if selected_secondary_therapist_id
                            else None
                        ),
                    )
                    for arr_id in extra_arrangement_ids
                ),
                return_exceptions=True,
            )
            for res in extra_results:
                if isinstance(res, GatewayTimeoutError):
                    raise HTTPException(
                        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                        detail=f"Availability check timed out: {str(res)}",
                    )
                if isinstance(res, HTTPException):
                    raise res
                if isinstance(res, Exception):
                    logger.error("check_availability_failed", error=str(res))
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Failed to check appointment availability: {str(res)}",
                    )
                if res.get("available") != "yes":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="One of the selected service arrangements is not available for the requested date and time.",
                    )

    # ── 4. Build start and end datetime ─────────────────────────────────
    # The customer's chosen date/time is Asia/Kuwait local time. Store it as a
    # real timezone-aware instant (e.g. 2026-10-10T10:00:00+03:00).
    if _session_plan is not None:
        # Multi-session: use the mirror values computed by the planner
        appointment_start = _session_plan.mirror["appointment_start"]
        appointment_end = _session_plan.mirror["appointment_end"]
    else:
        try:
            naive_dt = datetime.fromisoformat(f"{appointment_date}T{appointment_time}")
            appointment_start = to_local_tz(naive_dt)
        except Exception:
            appointment_start = to_local_tz(body.appointment_start) or local_now()
        appointment_end = appointment_start + timedelta(minutes=duration)

    # ── 5. Build pricing and addons ─────────────────────────────────────
    # Single service: legacy client price. Multi-service: sum of the services' prices.
    if _session_plan is not None:
        # Multi-session: server computes prices from session planner (source of truth)
        _svcs_price, _add_price_dec, _ext_price_dec = totals_as_decimals(_session_plan)
        _tot_price_dec = _svcs_price + _add_price_dec + _ext_price_dec
        arr_price = str(_svcs_price)
        add_price = str(_add_price_dec)
        ext_price = str(_ext_price_dec)
        tot_price = str(_tot_price_dec)
        curr = body.currency or "KWD"
        disc_price = str(
            body.discount
            or (body.pricing_details.get("discount") if body.pricing_details else None)
            or (body.pricing_details.get("discount_amount") if body.pricing_details else None)
            or "0"
        )
        pricing = PricingBreakdown(
            arrangement_price=Decimal(arr_price),
            price_for_extra_minutes=Decimal(ext_price),
            addon_price=Decimal(add_price),
            discount=Decimal(disc_price),
            tax=Decimal("0.000"),
            fees=Decimal("0.000"),
            total=Decimal(tot_price),
            currency=curr,
        )
    else:
        fallback_base = (
            body.base_price
            if len(service_entries) == 1
            else (total_services_price(service_entries) or None)
        )
        if body.pricing_details:
            arr_price = str(
                body.pricing_details.get("arrangement_price")
                or body.pricing_details.get("arrangement")
                or body.pricing_details.get("base_price")
                or body.pricing_details.get("base")
                or fallback_base
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
            arr_price = str(fallback_base or body.total_price or "0")
            add_price = str(body.addons_price or "0")
            ext_price = str(body.extra_price or "0")
            tot_price = str(body.total_price or fallback_base or "0")
            curr = body.currency or "KWD"

        disc_price = str(
            body.discount
            or (body.pricing_details.get("discount") if body.pricing_details else None)
            or (body.pricing_details.get("discount_amount") if body.pricing_details else None)
            or "0"
        )
        pricing = PricingBreakdown(
            arrangement_price=Decimal(arr_price),
            price_for_extra_minutes=Decimal(ext_price),
            addon_price=Decimal(add_price),
            discount=Decimal(disc_price),
            tax=Decimal("0.000"),
            fees=Decimal("0.000"),
            total=Decimal(tot_price),
            currency=curr,
        )

    # Course sessions are prepaid: zero price, no gateway, never loyalty-eligible.
    course_arrangement_type: str | None = None
    course_payment_fields: dict[str, Any] = {}
    if is_course_redemption:
        pricing = PricingBreakdown(
            arrangement_price=Decimal("0.000"),
            price_for_extra_minutes=Decimal("0.000"),
            addon_price=Decimal("0.000"),
            discount=Decimal("0.000"),
            tax=Decimal("0.000"),
            fees=Decimal("0.000"),
            total=Decimal("0.000"),
            currency=pricing.currency,
        )
        course_payment_fields = {
            "payment_id": None,
            "payment_provider": COURSE_PAYMENT_PROVIDER,
            "payment_gateway": None,
            "payment_through": body.payment_through or "ushdesk",
            "payment_method": COURSE_PAYMENT_METHOD,
            "payment_url": None,
        }
        for _entry in service_entries:
            _entry["service_data"] = {
                **(_entry.get("service_data") or {}),
                "is_eligible_for_loyalty": False,
                "loyalty_points": 0,
            }
        # Room type comes from ushauth (not from the client-supplied snapshot)
        _course_arr_id = (
            (service_entries[0].get("service_arrangement_id") if service_entries else None)
            or service_arrangement_id
        )
        if _course_arr_id:
            try:
                _arr_raw = await ushauth.get_service_arrangement(str(_course_arr_id))
                _arr = _arr_raw.get("data", _arr_raw) if isinstance(_arr_raw, dict) else {}
                course_arrangement_type = str(_arr.get("arrangement_type") or "") or None
            except Exception as exc:
                logger.warning("course_arrangement_lookup_failed", arrangement_id=str(_course_arr_id), error=str(exc))

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
    _is_home_booking = booking_type_str == "home_service"
    if _is_home_booking:
        # Home service bookings are not tied to a branch: store "Home Service" instead of the
        # therapist's branch name so lists / invoices / events show the right location.
        branch_name = "Home Service"
        branch_address = branch_address or "Home Service"
    branch_data = {
        "id": str(body.branch_id) if body.branch_id else "",
        "name": branch_name,
        "address": branch_address,
        "branch_name": branch_name,
        "branch_address": branch_address,
        **(body.branch_data or {}),
    }
    if _is_home_booking:
        branch_data["name"] = branch_name
        branch_data["branch_name"] = branch_name

    therapist_name = (
        body.therapist_name
        or therapist_display_name(body.therapist_data)
    )
    t_avatar = (body.therapist_data or {}).get("avatar") or (body.therapist_data or {}).get("photo_url")
    if (not therapist_name or not t_avatar) and selected_therapist_id:
        try:
            t_profile = await ushauth.get_therapist(str(selected_therapist_id))
            t_data_raw = t_profile.get("data") if isinstance(t_profile.get("data"), dict) else t_profile
            if not therapist_name:
                therapist_name = therapist_display_name(t_data_raw)
            if not t_avatar and isinstance(t_data_raw, dict):
                t_avatar = t_data_raw.get("avatar") or t_data_raw.get("photo_url")
            if not body.therapist_data and isinstance(t_data_raw, dict):
                body.therapist_data = t_data_raw
        except Exception as exc:
            logger.warning("therapist_lookup_failed", error=str(exc))

    therapist_data = {
        "id": str(selected_therapist_id),
        "name": therapist_name,
        "therapist_name": therapist_name,
        "full_name": therapist_name,
        **(body.therapist_data or {}),
    }
    if t_avatar:
        therapist_data.setdefault("avatar", t_avatar)
        therapist_data.setdefault("photo_url", t_avatar)

    # Snapshot of the second therapist (couple/double rooms only)
    secondary_therapist_data: dict | None = None
    if selected_secondary_therapist_id:
        secondary_matches_request = secondary_requested_id == str(selected_secondary_therapist_id)
        secondary_name = (
            (body.secondary_therapist_name or therapist_display_name(body.secondary_therapist_data))
            if secondary_matches_request
            else ""
        )
        sec_avatar = (
            ((body.secondary_therapist_data or {}).get("avatar") or (body.secondary_therapist_data or {}).get("photo_url"))
            if secondary_matches_request
            else None
        )
        if not secondary_name or not sec_avatar:
            try:
                s_profile = await ushauth.get_therapist(str(selected_secondary_therapist_id))
                s_data_raw = s_profile.get("data") if isinstance(s_profile.get("data"), dict) else s_profile
                if not secondary_name:
                    secondary_name = therapist_display_name(s_data_raw)
                if not sec_avatar and isinstance(s_data_raw, dict):
                    sec_avatar = s_data_raw.get("avatar") or s_data_raw.get("photo_url")
            except Exception as exc:  # name is cosmetic; never block the booking
                logger.warning("secondary_therapist_lookup_failed", error=str(exc))
        secondary_therapist_data = {
            "id": str(selected_secondary_therapist_id),
            "name": secondary_name,
            "therapist_name": secondary_name,
            "full_name": secondary_name,
            **((body.secondary_therapist_data or {}) if secondary_matches_request else {}),
        }
        if sec_avatar:
            secondary_therapist_data.setdefault("avatar", sec_avatar)
            secondary_therapist_data.setdefault("photo_url", sec_avatar)

    idempotency_key = body.idempotency_key or request.headers.get("Idempotency-Key")

    # ── Resolve invoice_number & payment_number ──────────────────────────
    resolved_invoice_number: str | None = body.invoice_number or (body.payment_data or {}).get("invoice_number") or (body.voucher_data or {}).get("invoice_number")
    resolved_payment_number: str | None = body.payment_number or (body.payment_data or {}).get("payment_number") or (body.voucher_data or {}).get("payment_number")
    if (not resolved_invoice_number or not resolved_payment_number) and body.payment_id:
        try:
            p_stmt = select(Payment.invoice_number, Payment.payment_number).where(
                (Payment.id == uuid.UUID(str(body.payment_id)))
                | (Payment.payment_id == str(body.payment_id))
            )
            p_res = await booking_service._session.execute(p_stmt)
            p_row = p_res.first()
            if p_row:
                if not resolved_invoice_number and p_row[0]:
                    resolved_invoice_number = p_row[0]
                if not resolved_payment_number and p_row[1]:
                    resolved_payment_number = p_row[1]
        except Exception:
            pass

    merged_payment_data = dict(body.payment_data or {})
    if resolved_payment_number:
        merged_payment_data["payment_number"] = resolved_payment_number
    if resolved_invoice_number:
        merged_payment_data["invoice_number"] = resolved_invoice_number

    # ── 7. Create booking record ────────────────────────────────────────
    try:
        booking = await booking_service.create_booking(
            customer_id=customer_id,
            customer_data=customer_data,
            branch_id=body.branch_id,
            branch_data=branch_data,
            services=service_entries,
            therapist_id=selected_therapist_id,
            therapist_data=therapist_data,
            secondary_therapist_id=selected_secondary_therapist_id,
            secondary_therapist_data=secondary_therapist_data,
            appointment_start=appointment_start,
            appointment_end=appointment_end,
            duration_minutes=total_services_duration(service_entries) or 60,
            extra_minutes=int(body.extra_minutes or 0),
            total_duration=body.total_duration,
            addons_duration=int(body.addons_duration or 0) if body.addons_duration is not None else None,
            base_price=Decimal(str(_svcs_price)) if _session_plan is not None else (Decimal(str(fallback_base)) if fallback_base is not None else None),
            pricing=pricing,
            addons=addon_records,
            sessions=_session_plan.sessions if _session_plan is not None else None,
            customer_notes=body.customer_notes or body.customer_message or None,
            payment_id=course_payment_fields.get("payment_id", body.payment_id),
            payment_provider=course_payment_fields.get("payment_provider", body.payment_provider),
            payment_gateway=course_payment_fields.get("payment_gateway", body.payment_gateway),
            payment_through=course_payment_fields.get("payment_through", body.payment_through),
            payment_method=course_payment_fields.get("payment_method", body.payment_method),
            payment_url=course_payment_fields.get("payment_url", body.payment_url),
            payment_data=merged_payment_data,
            idempotency_key=idempotency_key,
            booking_type=booking_type_str,
            payment_type=payment_type_str,
            status=final_status,
            payment_status=final_payment_status,
            loyalty_data=body.loyalty_data,
            reward_id=body.reward_id,
            voucher_id=body.voucher_id,
            voucher_data=body.voucher_data,
            invoice_number=resolved_invoice_number,
            created_by_user=created_by_user,
            created_by_user_data=created_by_user_data,
            created_by=created_by,
            source=request_source,
            **(
                {
                    "course_subscription_id": body.course_subscription_id,
                    "course_arrangement_type": course_arrangement_type,
                }
                if is_course_redemption
                else {}
            ),
        )

        # Link any existing Payment record to this new booking
        try:
            if body.payment_id:
                await booking_service._session.execute(
                    update(Payment)
                    .where(
                        (Payment.id == uuid.UUID(str(body.payment_id)))
                        | (Payment.payment_id == str(body.payment_id))
                    )
                    .values(booking_id=booking.id)
                )
                await booking_service._session.commit()
            elif resolved_payment_number:
                await booking_service._session.execute(
                    update(Payment)
                    .where(Payment.payment_number == resolved_payment_number)
                    .values(booking_id=booking.id)
                )
                await booking_service._session.commit()
        except Exception:
            pass

    except DoubleBookingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
        )
    except CourseError as exc:
        raise _course_http_error(exc)
    except USHBaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        )

    # ── Deduct loyalty points if this is a loyalty booking ──────────────
    if is_loyalty_booking or booking_type_str == "loyalty":
        points_to_deduct = (
            entries_points_cost
            or (int(body.price_in_points) if getattr(body, "price_in_points", None) else 0)
            or (int(body.loyalty_points) if body.loyalty_points else 0)
            or (int(body.loyalty_data.get("points_cost", 0)) if body.loyalty_data else 0)
        )
        if points_to_deduct > 0:
            from app.loyalty.application.services import LoyaltyService
            loyalty_svc = LoyaltyService(booking_service._session)
            try:
                await loyalty_svc.redeem_points(
                    customer_id=customer_id,
                    cost_in_points=points_to_deduct,
                    booking_id=booking.id,
                    booking_number=getattr(booking, "booking_number", None),
                    created_by=created_by,
                )
            except ValueError as exc:
                logger.error("loyalty_redeem_failed", customer_id=str(customer_id), error=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                )

    # ── Auto-create or fetch invoice from ushanr only when payment_status=success or loyalty booking ─
    inv_id_res: str | None = None
    inv_num_res: str | None = getattr(booking, "invoice_number", None)
    is_voucher_redemption = (
        booking_type_str == "gift_voucher"
        or payment_type_str == "gift_voucher"
        or body.voucher_id is not None
        or (body.payment_method or "").lower() in ("voucher", "gift_voucher")
    )
    _booking_pstatus = str(booking.payment_status or "").strip().lower()
    is_loyalty = (
        is_loyalty_booking
        or booking_type_str == "loyalty"
        or _booking_pstatus == "rewarded"
        or bool(body.loyalty_data)
    )
    if (
        not inv_num_res
        and not is_voucher_redemption
        and not is_course_redemption
        and (_booking_pstatus in (PaymentStatus.SUCCESS.value, "success", "paid") or is_loyalty)
    ):
        from app.integrations.ushanr_client import UshanrClient
        ushanr_c = UshanrClient()
        try:
            cust_name = f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()
            inv_data = await ushanr_c.create_or_get_booking_invoice_data(
                booking=booking,
                customer_name=cust_name,
                customer_phone=customer_data.get("phone_number", ""),
                customer_email=customer_data.get("email", ""),
            )
            if inv_data:
                inv_num_res = inv_data.get("invoice_number")
                inv_id_res = inv_data.get("invoice_id")
                if inv_num_res:
                    booking.invoice_number = inv_num_res
                    await booking_service._session.commit()
                # Also link to any payments for this booking
                try:
                    await booking_service._session.execute(
                        update(Payment)
                        .where(Payment.booking_id == booking.id)
                        .values(invoice_number=inv_num_res, invoice_id=inv_id_res)
                    )
                    await booking_service._session.commit()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("auto_create_booking_invoice_failed", booking_id=str(booking.id), error=str(exc))
        finally:
            await ushanr_c.aclose()
    elif inv_num_res:
        from app.integrations.ushanr_client import UshanrClient
        ushanr_c = UshanrClient()
        try:
            existing = await ushanr_c.get_invoice_by_source("booking", str(booking.id))
            if existing and existing.get("invoice_id"):
                inv_id_res = str(existing.get("invoice_id"))
        except Exception:
            pass
        finally:
            await ushanr_c.aclose()

    # ── For loyalty bookings: create payment record with zero amount and connect invoice ──
    if is_loyalty and not is_course_redemption:
        try:
            p_stmt = select(Payment).where(Payment.booking_id == booking.id)
            p_res = await booking_service._session.execute(p_stmt)
            existing_p = p_res.scalars().first()

            computed_dur = int(
                getattr(booking, "total_duration", None)
                or (
                    booking.duration_minutes
                    + (booking.extra_minutes or 0)
                    + (getattr(booking, "addons_duration", None) or 0)
                )
                or 60
            )

            if existing_p:
                if inv_num_res:
                    existing_p.invoice_number = inv_num_res
                if inv_id_res:
                    existing_p.invoice_id = inv_id_res
                existing_p.total_amount = Decimal("0")
                existing_p.invoice_value = Decimal("0")
                existing_p.status = "success"
                if not existing_p.payment_provider:
                    existing_p.payment_provider = "Loyalty Points"
                if not existing_p.payment_through:
                    existing_p.payment_through = "ushspa"
                if not existing_p.payment_method:
                    existing_p.payment_method = "loyalty_points"
                p_rec = existing_p
            else:
                from app.api.v1.payments import generate_payment_number
                pmt_num = await generate_payment_number(booking_service._session)
                p_rec = Payment(
                    payment_number=pmt_num,
                    booking_id=booking.id,
                    customer_id=booking.customer_id,
                    customer_data=booking.customer_data,
                    service_id=booking.service_id,
                    service_data=booking.service_data,
                    branch_id=booking.branch_id,
                    branch_data=booking.branch_data,
                    service_arrangement_id=booking.service_arrangement_id,
                    service_arrangement_data=booking.service_arrangement_data,
                    total_amount=Decimal("0"),
                    total_duration=computed_dur,
                    currency=booking.currency or "KWD",
                    status="success",
                    payment_for="branch_service",
                    payment_provider="Loyalty Points",
                    payment_through="ushspa",
                    payment_gateway="Other",
                    payment_method="loyalty_points",
                    invoice_number=inv_num_res,
                    invoice_id=inv_id_res,
                    invoice_value=Decimal("0"),
                    payment_data={
                        **(merged_payment_data or {}),
                        "booking_type": "loyalty",
                        "invoice_number": inv_num_res,
                        "invoice_id": inv_id_res,
                        "points_cost": (
                            entries_points_cost
                            or (int(body.price_in_points) if getattr(body, "price_in_points", None) else 0)
                            or (int(body.loyalty_points) if body.loyalty_points else 0)
                            or (int(body.loyalty_data.get("points_cost", 0)) if body.loyalty_data else 0)
                        ),
                    },
                    created_by_user=str(created_by_user or created_by or "customer"),
                    created_by_user_data=created_by_user_data,
                )
                booking_service._session.add(p_rec)
                await booking_service._session.flush()

                p_hist = PaymentStatusHistory(
                    payment_id=p_rec.id,
                    old_status=None,
                    new_status="success",
                    source="ushspa",
                    reason="Loyalty reward booking - payment completed with zero amount",
                    change_by_user=str(created_by_user or created_by or "customer"),
                    change_by_user_data=created_by_user_data,
                    correlation_id=str(p_rec.id),
                )
                booking_service._session.add(p_hist)

            booking.payment_id = p_rec.id
            booking_pdata = dict(booking.payment_data or {})
            if p_rec.payment_number:
                booking_pdata["payment_number"] = p_rec.payment_number
            if inv_num_res:
                booking_pdata["invoice_number"] = inv_num_res
            if inv_id_res:
                booking_pdata["invoice_id"] = inv_id_res
            booking.payment_data = booking_pdata
            await booking_service._session.commit()
        except Exception as p_err:
            logger.warning("auto_create_loyalty_payment_failed", booking_id=str(booking.id), error=str(p_err))

    # ── 7b. Create appointment_cache in ushauth immediately on booking creation ──
    try:
        appt_date_str = appointment_start.strftime("%Y-%m-%d")
        appt_time_str = appointment_start.strftime("%H:%M:%S")
        cache_booking_type = "home" if booking_type_str in ("home_service", "home") else "branch"
        cust_name = f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()

        _initial_cache_status = "confirmed" if str(booking.status or "").lower() in ("confirmed", "draft") else str(booking.status or "confirmed").lower()
        _initial_cache_payment_status = "paid" if str(booking.payment_status or "").lower() in ("success", "paid") else "pending"

        await ushauth.create_appointment_cache(
            service_arrangement_id=str(body.service_arrangement_id) if body.service_arrangement_id else None,
            therapist_id=str(selected_therapist_id),
            booking_id=str(booking.id),
            appointment_date=appt_date_str,
            appointment_time=appt_time_str,
            booking_type=cache_booking_type,
            duration=duration,
            status=_initial_cache_status,
            payment_status=_initial_cache_payment_status,
            customer_id=str(customer_id) if customer_id else None,
            customer_name=cust_name or None,
            branch_id=str(body.branch_id) if body.branch_id else None,
            branch_name=branch_name or None,
            service_id=service_entries[0]["service_id"] or None,
            service_name=service_name or None,
            services=cache_services_payload(service_entries),
            secondary_therapist_id=str(selected_secondary_therapist_id) if selected_secondary_therapist_id else None,
            secondary_therapist_name=(secondary_therapist_data or {}).get("name") or None,
            sessions=cache_sessions_payload(_session_plan.sessions) if _session_plan is not None else None,
        )
        logger.info(
            "appointment_cache_created_for_booking",
            booking_id=str(booking.id),
            status=_initial_cache_status,
            payment_status=_initial_cache_payment_status,
        )
    except Exception as exc:
        logger.warning(
            "appointment_cache_creation_failed_for_booking",
            booking_id=str(booking.id),
            error=str(exc),
        )

    # ── 8. Return success response ──────────────────────────────────────
    return CreateBookingResponse(
        success=True,
        data=CreateBookingDataResponse(
            booking_id=str(booking.id),
            booking_number=_safe_str(getattr(booking, "booking_number", None)),
            customer_id=str(booking.customer_id),
            services=_services_out(booking),
            final_amount=str(booking.total_amount),
            total_amount=str(booking.total_amount),
            total_price=str(booking.total_amount),
            discount=str(booking.discount),
            arrangement_price=str(booking.arrangement_price),
            addon_price=str(booking.addon_price),
            currency=str(booking.currency) if isinstance(getattr(booking, "currency", None), str) else "KWD",
            pricing=PricingBreakdownSchema(
                arrangement_price=str(booking.arrangement_price),
                price_for_extra_minutes=str(booking.price_for_extra_minutes),
                addon_price=str(booking.addon_price),
                discount=str(booking.discount),
                tax=str(booking.tax),
                fees=str(booking.fees),
                total=str(booking.total_amount),
                currency=str(booking.currency) if isinstance(getattr(booking, "currency", None), str) else "KWD",
            ),
            status=booking.status,
            payment_status=booking.payment_status,
            payment_id=_safe_str(getattr(booking, "payment_id", None)),
            payment_provider=_safe_str(getattr(booking, "payment_provider", None)),
            payment_gateway=_safe_str(getattr(booking, "payment_gateway", None)),
            payment_through=_safe_str(getattr(booking, "payment_through", None)),
            payment_method=_safe_str(getattr(booking, "payment_method", None)),
            payment_url=_safe_str(getattr(booking, "payment_url", None)),
            payment_type=booking.payment_type,
            payment_data=booking.payment_data,
            is_eligible_for_loyalty=bool((booking.service_data or {}).get("is_eligible_for_loyalty", False)),
            loyalty_data=getattr(booking, "loyalty_data", None),
            reward_id=str(booking.reward_id) if getattr(booking, "reward_id", None) else None,
            voucher_id=str(booking.voucher_id) if getattr(booking, "voucher_id", None) else None,
            voucher_data=getattr(booking, "voucher_data", None),
            course_subscription_id=_course_subscription_id_out(booking),
            course_data=_course_data_out(booking),
            created_by_user=_safe_str(getattr(booking, "created_by_user", None) or getattr(booking, "created_by", None)),
            created_by_user_data=getattr(booking, "created_by_user_data", None) if isinstance(getattr(booking, "created_by_user_data", None), dict) else None,
            created_by=_safe_str(getattr(booking, "created_by_user", None) or getattr(booking, "created_by", None)),
            invoice_number=_safe_str(inv_num_res or getattr(booking, "invoice_number", None)),
            invoice_id=inv_id_res,
            payment_number=_safe_str(resolved_payment_number or (booking.payment_data or {}).get("payment_number")),
        ),
    )




@router.get(
    "/",
    response_model=None,
    summary="List all bookings (staff with filters)",
    description=(
        "Staff endpoint to retrieve and filter bookings by status, payment_status, date, "
        "customer, branch, therapist, service, etc. Supports pagination. "
        "Requires 'bookings.list' permission for employee accounts."
    ),
)
async def list_bookings(
    booking_service: BookingServiceDep,
    request: Request,
    current_user: CurrentUser,
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
    """List bookings with filtering and pagination. Requires bookings.list permission."""
    is_therapist = _is_therapist_user(current_user)

    # Enforce RBAC for employee/admin users; customers use list_my_bookings instead.
    # Therapists are exempt: they are locked to their own bookings (today → +7 days) below.
    if current_user.user_type in ("employee", "admin") and not is_therapist:
        if not current_user.has_permission("bookings.list"):
            raise HTTPException(
                status_code=403,
                detail={"code": "PERMISSION_DENIED", "message": "Permission 'bookings.list' is required."},
            )

    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 20)

    if is_therapist:
        # Hard lock: own bookings only, within [today, today+7]; client-supplied values are ignored.
        win_from, win_to = _therapist_window_dates()
        req_from, req_to, req_date = _val(from_date), _val(to_date), _val(date)
        if req_date and win_from <= str(req_date)[:10] <= win_to:
            req_from = req_to = None
        else:
            req_date = None
        from_date = max(str(req_from)[:10], win_from) if req_from else win_from
        to_date = min(str(req_to)[:10], win_to) if req_to else win_to
        date = req_date
        therapist_id = uuid.UUID(_therapist_key(current_user))
        customer_id = None
        search = None

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
    current_user: OptionalUser = None,
) -> JSONResponse:
    """Get full detail of a booking. Requires only the USHSPA app token (public)."""
    booking = await booking_service.get_booking(booking_id, load_history=True)

    # Therapists may only open bookings they are assigned to.
    if _is_therapist_user(current_user) and not _booking_involves_therapist(booking, _therapist_key(current_user)):
        raise HTTPException(
            status_code=403,
            detail={"code": "PERMISSION_DENIED", "message": "You can only view your own bookings."},
        )

    return JSONResponse(
        content={"success": True, "data": _booking_to_detail(booking).model_dump(mode="json")}
    )


class ChangeSessionTherapistRequest(BaseModel):
    """Re-assign the therapist(s) of one session of a booking."""

    therapist_id: uuid.UUID | None = None
    therapist_data: dict | None = None
    secondary_therapist_id: uuid.UUID | None = None
    secondary_therapist_data: dict | None = None


@router.patch(
    "/{booking_id}/sessions/{session_id}/therapist/",
    summary="Change the therapist of a single session",
    response_model=None,
)
async def change_session_therapist(
    booking_id: uuid.UUID,
    session_id: str,
    body: ChangeSessionTherapistRequest,
    current_user: CurrentUser,
    booking_service: BookingServiceDep,
    ushauth: USHAuthDep,
) -> JSONResponse:
    """Change therapist of one session; only that session's window is availability-checked."""
    _deny_therapist(current_user)
    therapist_data = body.therapist_data
    if body.therapist_id and not therapist_data:
        try:
            t_raw = await ushauth.get_therapist(str(body.therapist_id))
            therapist_data = t_raw.get("data") if isinstance(t_raw.get("data"), dict) else t_raw
        except Exception:
            pass
    if therapist_data and isinstance(therapist_data, dict):
        t_name = therapist_display_name(therapist_data)
        if t_name:
            therapist_data.setdefault("name", t_name)
            therapist_data.setdefault("therapist_name", t_name)
            therapist_data.setdefault("full_name", t_name)

    secondary_data = body.secondary_therapist_data
    if body.secondary_therapist_id and not secondary_data:
        try:
            s_raw = await ushauth.get_therapist(str(body.secondary_therapist_id))
            secondary_data = s_raw.get("data") if isinstance(s_raw.get("data"), dict) else s_raw
        except Exception:
            pass
    if secondary_data and isinstance(secondary_data, dict):
        s_name = therapist_display_name(secondary_data)
        if s_name:
            secondary_data.setdefault("name", s_name)
            secondary_data.setdefault("therapist_name", s_name)
            secondary_data.setdefault("full_name", s_name)

    try:
        updated = await booking_service.change_session_therapist(
            booking_id,
            session_id,
            therapist_id=body.therapist_id,
            therapist_data=therapist_data,
            secondary_therapist_id=body.secondary_therapist_id,
            secondary_therapist_data=secondary_data,
            secondary_provided="secondary_therapist_id" in body.model_fields_set,
        )
    except DoubleBookingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except CoreValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # Keep the ushauth appointment cache in sync (per-session rows)
    try:
        await ushauth.update_appointment_cache_by_booking_id(
            booking_id=str(booking_id),
            therapist_id=str(updated.therapist_id) if updated.therapist_id else None,
            sessions=cache_sessions_payload(get_booking_sessions(updated)),
        )
    except Exception as exc:
        logger.warning(
            "appointment_cache_update_on_session_therapist_change_failed",
            booking_id=str(booking_id),
            error=str(exc),
        )

    return JSONResponse(
        content={"success": True, "data": _booking_to_detail(updated).model_dump(mode="json")}
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
    _deny_therapist(current_user)
    booking = await booking_service.get_booking(booking_id)
    prev_secondary_therapist = _secondary_therapist_id_out(booking)
    was_parked = str(booking.status or "").lower() == BookingStatus.PARKED.value
    parked_time_changed = True  # refined below when sessions are supplied

    # Check permission if called by non-admin customer
    user_id = uuid.UUID(current_user.sub)
    if body.source == "customer" and booking.customer_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You can only update your own bookings.",
        )

    therapist_data = body.therapist_data
    if body.therapist_id and not therapist_data:
        try:
            t_raw = await ushauth.get_therapist(str(body.therapist_id))
            therapist_data = t_raw.get("data") if isinstance(t_raw.get("data"), dict) else t_raw
        except Exception:
            pass
    if therapist_data and isinstance(therapist_data, dict):
        t_name = therapist_display_name(therapist_data)
        if t_name:
            therapist_data.setdefault("name", t_name)
            therapist_data.setdefault("therapist_name", t_name)
            therapist_data.setdefault("full_name", t_name)

    # Second therapist (couple_room / double_room bookings)
    secondary_therapist_data = body.secondary_therapist_data
    if body.secondary_therapist_id and not secondary_therapist_data:
        try:
            s_raw = await ushauth.get_therapist(str(body.secondary_therapist_id))
            secondary_therapist_data = s_raw.get("data") if isinstance(s_raw.get("data"), dict) else s_raw
        except Exception:
            pass
    if secondary_therapist_data and isinstance(secondary_therapist_data, dict):
        s_name = therapist_display_name(secondary_therapist_data)
        if s_name:
            secondary_therapist_data.setdefault("name", s_name)
            secondary_therapist_data.setdefault("therapist_name", s_name)
            secondary_therapist_data.setdefault("full_name", s_name)

    branch_data = body.branch_data
    if body.branch_id and not branch_data:
        try:
            b_raw = await ushauth.get_branch(str(body.branch_id))
            branch_data = b_raw.get("data") if isinstance(b_raw.get("data"), dict) else b_raw
        except Exception:
            pass

    service_arrangement_data = body.service_arrangement_data
    if body.service_arrangement_id and not service_arrangement_data:
        try:
            a_raw = await ushauth.get_service_arrangement(str(body.service_arrangement_id))
            service_arrangement_data = a_raw.get("data") if isinstance(a_raw.get("data"), dict) else a_raw
        except Exception:
            pass

    # Multi-service: replace the booked services (same branch + therapist)
    new_service_entries: list[dict[str, Any]] | None = None
    if body.services:
        try:
            new_service_entries = await resolve_service_entries(
                ushauth,
                body.services,
                is_loyalty_booking=str(getattr(booking, "booking_type", "")) == "loyalty",
            )
        except ServiceValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )

    # ── Multi-session update path ─────────────────────────────────────────
    _upd_session_plan: SessionPlan | None = None
    if body.sessions:
        _upd_appt_date = (
            body.appointment_date
            or (booking.appointment_start.strftime("%Y-%m-%d") if booking.appointment_start else None)
        )
        if _upd_appt_date is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="appointment_date is required when updating sessions.",
            )
        try:
            _plan_kwargs = dict(
                appointment_date=_upd_appt_date,
                is_loyalty_booking=str(getattr(booking, "booking_type", "")) == "loyalty",
                branch_id=str(body.branch_id) if body.branch_id else str(getattr(booking, "branch_id", "") or ""),
                exclude_booking_id=str(booking_id),
            )
            if was_parked:
                # Parked bookings hold no slot. Only a real change of the session start times
                # (a reschedule) re-activates the booking and needs an availability check.
                _upd_session_plan = await plan_sessions(ushauth, body.sessions, check_availability=False, **_plan_kwargs)
                try:
                    _old_starts = [session_window(s)[0] for s in get_booking_sessions(booking)]
                    _new_starts = [session_window(s)[0] for s in _upd_session_plan.sessions]
                    parked_time_changed = _old_starts != _new_starts
                except Exception:
                    parked_time_changed = True
                if parked_time_changed:
                    _upd_session_plan = await plan_sessions(ushauth, body.sessions, check_availability=True, **_plan_kwargs)
            else:
                _upd_session_plan = await plan_sessions(ushauth, body.sessions, check_availability=True, **_plan_kwargs)
        except SessionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        except SessionAvailabilityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
        new_service_entries = _upd_session_plan.service_entries

    target_start = to_local_tz(body.appointment_start)
    if _upd_session_plan is not None:
        target_start = _upd_session_plan.mirror["appointment_start"]
    elif target_start is None and body.appointment_date and body.appointment_time:
        try:
            naive_dt = datetime.fromisoformat(f"{body.appointment_date}T{body.appointment_time}")
            target_start = to_local_tz(naive_dt)
        except Exception:
            pass

    # Rescheduling a parked booking (new date/time or sessions) re-activates it
    _effective_status = body.status
    if was_parked and not body.status and parked_time_changed and (
        body.sessions or body.appointment_start or (body.appointment_date and body.appointment_time)
    ):
        _effective_status = BookingStatus.CONFIRMED

    updated = await booking_service.update_booking(
        booking_id=booking_id,
        status=_effective_status,
        payment_status=body.payment_status,
        payment_id=body.payment_id,
        payment_provider=body.payment_provider,
        payment_gateway=body.payment_gateway,
        payment_through=body.payment_through,
        payment_method=body.payment_method,
        payment_url=body.payment_url,
        payment_type=body.payment_type,
        payment_data=body.payment_data,
        created_by_user=body.created_by_user or body.created_by,
        created_by_user_data=body.created_by_user_data,
        therapist_id=body.therapist_id,
        therapist_data=therapist_data,
        secondary_therapist_id=body.secondary_therapist_id,
        secondary_therapist_data=secondary_therapist_data,
        secondary_therapist_provided="secondary_therapist_id" in body.model_fields_set,
        branch_id=body.branch_id,
        branch_data=branch_data,
        service_arrangement_id=body.service_arrangement_id,
        service_arrangement_data=service_arrangement_data,
        services=new_service_entries,
        appointment_start=target_start,
        extra_minutes=body.extra_minutes,
        customer_notes=body.customer_notes,
        internal_notes=body.internal_notes,
        reason=body.reason,
        source=body.source,
        changed_by=str(body.change_by_user or body.created_by_user or body.created_by or user_id),
        change_by_user_data=body.change_by_user_data or body.created_by_user_data or {
            "id": current_user.sub,
            "first_name": current_user.first_name or "",
            "last_name": current_user.last_name or "",
            "phone_number": current_user.phone_number or "",
            "email": current_user.email or "",
            "user_type": _safe_str(getattr(current_user, "user_type", None)) or "customer",
            "role": _safe_str(getattr(current_user, "role", None)),
        },
        voucher_id=body.voucher_id,
        voucher_data=body.voucher_data,
        invoice_number=body.invoice_number,
        addons=body.addons or body.selected_addons,
        addon_price=body.addon_price or body.addons_price,
        addons_duration=body.addons_duration,
        price_for_extra_minutes=body.price_for_extra_minutes or body.extra_price,
        total_duration=body.total_duration,
        total_amount=body.total_amount or body.total_price,
        sessions=_upd_session_plan.sessions if _upd_session_plan is not None else None,
    )

    if body.payment_number:
        pdata = dict(updated.payment_data or {})
        pdata["payment_number"] = body.payment_number
        updated.payment_data = pdata
        await booking_service._session.commit()


    # ── Dispatch booking.updated SQS event to ushnotice ───────────────────
    appt_dt_str = to_local_tz(updated.appointment_start).strftime("%Y-%m-%d") if updated.appointment_start else ""
    appt_time_str = to_local_tz(updated.appointment_start).strftime("%H:%M:%S") if updated.appointment_start else ""
    duration_val = int(updated.duration_minutes + (updated.extra_minutes or 0))

    try:
        ev_data = _build_booking_event_data(updated)
    except Exception as ev_build_exc:
        logger.warning("booking_updated_build_event_data_warn", booking_id=str(booking_id), error=str(ev_build_exc))
        ev_data = {}

    await booking_service._enqueue_event(
        BookingUpdatedEvent(
            booking_id=str(booking_id),
            customer_id=str(updated.customer_id),
            appointment_date=appt_dt_str,
            appointment_time=appt_time_str,
            duration=duration_val,
            therapist_id=str(updated.therapist_id) if updated.therapist_id else "",
            secondary_therapist_id=str(ev_data.get("secondary_therapist_id") or _secondary_therapist_id_out(updated) or ""),
            secondary_therapist_name=str(ev_data.get("secondary_therapist_name") or ""),
            secondary_therapist_data=ev_data.get("secondary_therapist_data") or (_secondary_therapist_data_out(updated) or {}),
            status=str(updated.status) if updated.status else "",
            payment_status=str(updated.payment_status) if updated.payment_status else "",
            branch_id=str(updated.branch_id) if updated.branch_id else "",
            service_arrangement_id=str(updated.service_arrangement_id) if updated.service_arrangement_id else "",
            services=ev_data.get("services") or [],
            source=body.source or "",
            reason=body.reason or "",
            booking_number=str(ev_data.get("booking_number") or getattr(updated, "booking_number", "") or ""),
            booking_reference=str(ev_data.get("booking_reference") or getattr(updated, "booking_number", "") or str(booking_id)),
            customer_name=str(ev_data.get("customer_name") or ""),
            customer_phone=str(ev_data.get("customer_phone") or ""),
            customer_email=str(ev_data.get("customer_email") or ""),
            language_preference=str(ev_data.get("language_preference") or ""),
            customer_data=ev_data.get("customer_data") or (updated.customer_data if isinstance(updated.customer_data, dict) else {}),
            service_name=str(ev_data.get("service_name") or ""),
            branch_name=str(ev_data.get("branch_name") or ""),
            therapist_name=str(ev_data.get("therapist_name") or ""),
            total_amount=str(ev_data.get("total_amount") or str(updated.total_amount)),
            currency=str(ev_data.get("currency") or updated.currency or "KWD"),
            appointment_start=str(ev_data.get("appointment_start") or ""),
            appointment_end=str(ev_data.get("appointment_end") or ""),
            appointment_endtime=str(ev_data.get("appointment_endtime") or ""),
        )
    )

    # ── Sync appointment cache in ushauth directly ─────────────────────────
    # A parked booking has no cache rows (its slot is released), nothing to sync.
    try:
        if str(updated.status or "").lower() == BookingStatus.PARKED.value:
            raise RuntimeError("booking is parked – appointment cache sync skipped")
        _b_status = str(updated.status).lower().strip() if updated.status else None
        _p_status = str(updated.payment_status).lower().strip() if updated.payment_status else None
        _STATUS_NORM = {
            "draft": "confirmed",
            "payment_pending": "confirmed",
            "requested": "pending",
            "reschedule_requested": "pending",
            "reschedule_approved": "confirmed",
            "payment_failed": "cancelled",
            "no_show": "completed",
        }
        _PAYMENT_STATUS_NORM = {
            "not_initiated": "unpaid",
            "initiated": "pending",
            "payment_pending": "pending",
            "paid": "success",
        }
        cache_status = _STATUS_NORM.get(_b_status, _b_status) if _b_status else None
        if _p_status:
            cache_payment_status = _PAYMENT_STATUS_NORM.get(_p_status, _p_status)
        elif _b_status in ("payment_pending", "draft"):
            cache_payment_status = "pending"
        else:
            cache_payment_status = None

        _cache_extra: dict[str, Any] = {}
        _updated_secondary = _secondary_therapist_id_out(updated)
        if _updated_secondary or prev_secondary_therapist or "secondary_therapist_id" in body.model_fields_set:
            # A UUID sets/moves the second therapist row, null removes it
            _cache_extra["secondary_therapist_id"] = _updated_secondary
            _cache_extra["secondary_therapist_name"] = (
                (_secondary_therapist_data_out(updated) or {}).get("name") or None
            )

        await ushauth.update_appointment_cache_by_booking_id(
            booking_id=str(booking_id),
            appointment_date=appt_dt_str,
            appointment_time=appt_time_str,
            duration=duration_val,
            therapist_id=str(updated.therapist_id) if updated.therapist_id else None,
            status=cache_status,
            payment_status=cache_payment_status,
            branch_id=str(updated.branch_id) if updated.branch_id else None,
            service_arrangement_id=str(updated.service_arrangement_id) if updated.service_arrangement_id else None,
            services=cache_services_payload(get_booking_services(updated)),
            sessions=cache_sessions_payload(_upd_session_plan.sessions) if _upd_session_plan is not None else (cache_sessions_payload(get_booking_sessions(updated)) if get_booking_sessions(updated) else None),
            **_cache_extra,
        )
        logger.info(
            "appointment_cache_updated_on_booking_update",
            booking_id=str(booking_id),
            appointment_date=appt_dt_str,
            appointment_time=appt_time_str,
        )
    except Exception as exc:
        logger.warning(
            "appointment_cache_update_on_booking_update_failed",
            booking_id=str(booking_id),
            error=str(exc),
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

    user_id = str(body.change_by_user or current_user.sub)
    user_info = body.change_by_user_data or {
        "id": current_user.sub,
        "first_name": current_user.first_name or "",
        "last_name": current_user.last_name or "",
        "phone_number": current_user.phone_number or "",
        "email": current_user.email or "",
        "role": getattr(current_user, "role", None),
    }

    updated = await booking_service.cancel_booking(
        booking_id,
        reason=body.reason,
        cancelled_by=user_id,
        change_by_user=user_id,
        change_by_user_data=user_info,
        correlation_id=None,
    )

    refund_num = _safe_str(getattr(updated, "refund_number", None))
    if not refund_num:
        try:
            from app.payment.infrastructure.models import Refund
            from sqlalchemy import select
            r_stmt = select(Refund.refund_number).where(Refund.booking_id == booking_id)
            r_res = await booking_service._session.execute(r_stmt)
            refund_num = r_res.scalars().first()
        except Exception:
            pass

    return JSONResponse(
        content={
            "success": True,
            "data": {
                "booking_id": str(updated.id),
                "status": updated.status,
                "payment_status": updated.payment_status,
                "refund_number": refund_num,
            },
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

    user_id = str(body.change_by_user or current_user.sub)
    user_info = body.change_by_user_data or {
        "id": current_user.sub,
        "first_name": current_user.first_name or "",
        "last_name": current_user.last_name or "",
        "phone_number": current_user.phone_number or "",
        "email": current_user.email or "",
        "role": getattr(current_user, "role", None),
    }

    updated = await booking_service.request_reschedule(
        booking_id,
        new_start=body.new_start,
        new_end=new_end,
        customer_id=customer_id,
        change_by_user=user_id,
        change_by_user_data=user_info,
        source="customer",
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
    request: Request = None,
    current_user: OptionalUser = None,
) -> JSONResponse:
    """Update booking status and dispatch SQS event."""
    # Therapists: own bookings only, and the only transition they may perform is → completed.
    if _is_therapist_user(current_user):
        _target = str(getattr(body.status, "value", body.status) or "").strip().lower()
        if _target != "completed":
            raise HTTPException(
                status_code=403,
                detail={"code": "PERMISSION_DENIED", "message": "Therapists may only mark bookings as completed."},
            )
        _owned = await booking_service.get_booking(booking_id)
        if not _booking_involves_therapist(_owned, _therapist_key(current_user)):
            raise HTTPException(
                status_code=403,
                detail={"code": "PERMISSION_DENIED", "message": "You can only update your own bookings."},
            )
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

    user_id = (
        getattr(body, "change_by_user", None)
        or getattr(body, "changed_by", None)
        or getattr(body, "created_by_user", None)
    )
    user_data = (
        getattr(body, "change_by_user_data", None)
        or getattr(body, "created_by_user_data", None)
    )
    if not user_id and request is not None:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            try:
                from app.core.security import decode_token
                from app.core.config import get_settings
                settings = get_settings()
                payload = decode_token(auth_header.split(" ")[1], settings)
                user_id = payload.sub
                payload_user_type = _safe_str(getattr(payload, "user_type", None)) or "customer"
                payload_role = _safe_str(getattr(payload, "role", None))
                if not user_data:
                    full_name = f"{payload.first_name or ''} {payload.last_name or ''}".strip()
                    user_data = {
                        "id": payload.sub,
                        "name": full_name or getattr(payload, "name", None) or payload.sub,
                        "full_name": full_name or getattr(payload, "name", None) or payload.sub,
                        "first_name": payload.first_name or "",
                        "last_name": payload.last_name or "",
                        "phone_number": payload.phone_number or "",
                        "email": payload.email or "",
                        "user_type": payload_user_type,
                        "role": payload_role,
                    }
                elif isinstance(user_data, dict):
                    user_data = dict(user_data)
                    if "user_type" not in user_data:
                        user_data["user_type"] = payload_user_type
                    if "role" not in user_data:
                        user_data["role"] = payload_role
            except Exception:
                pass
    if not user_id:
        user_id = (
            (user_data.get("full_name") or user_data.get("name"))
            if isinstance(user_data, dict)
            else None
        ) or body.source or "system"
    if not user_data:
        user_data = {"source": body.source, "reason": body.reason} if body.source else None

    updated = await booking_service.update_status(
        booking_id=booking_id,
        new_status=body.status,
        payment_status=payment_status,
        payment_id=body.payment_id,
        payment_provider=body.payment_provider,
        payment_gateway=body.payment_gateway,
        payment_through=body.payment_through,
        payment_method=body.payment_method,
        payment_url=body.payment_url,
        payment_type=body.payment_type,
        payment_data=body.payment_data,
        reason=body.reason,
        source=body.source,
        changed_by=user_id,
        change_by_user_data=user_data,
        loyalty_data=body.loyalty_data,
        reward_id=body.reward_id,
        voucher_id=body.voucher_id,
        voucher_data=body.voucher_data,
        invoice_number=body.invoice_number,
    )

    if body.payment_number:
        pdata = dict(updated.payment_data or {})
        pdata["payment_number"] = body.payment_number
        updated.payment_data = pdata
        await booking_service._session.commit()


    # ── Update appointment cache when booking is cancelled ───────────────
    if body.status and str(body.status).lower() == "cancelled":
        try:
            raw_ps = (
                payment_status.value
                if payment_status
                else (
                    "refunded"
                    if (
                        updated.payment_status in ("refunded", "success", "paid")
                        or (updated.payment_data and updated.payment_data.get("is_paid"))
                    )
                    else "cancelled"
                )
            )
            cache_payment_status = "refunded" if raw_ps in ("refunded", "success", "paid") else "cancelled"
            await ushauth.update_appointment_cache_status_by_booking_id(
                booking_id=str(booking_id),
                new_status="cancelled",
                payment_status=cache_payment_status,
            )
            logger.info(
                "appointment_cache_updated_on_booking_cancellation",
                booking_id=str(booking_id),
                new_status="cancelled",
                payment_status=cache_payment_status,
            )
        except Exception as exc:
            logger.warning(
                "appointment_cache_update_on_cancellation_failed",
                booking_id=str(booking_id),
                error=str(exc),
            )

    # ── Dispatch desk-payment success event ───────────────────────────────
    # When a booking is paid manually at the desk the app sends:
    #   payment_status=success, source="ushspa app", reason="Paid on desk"
    # We fire a dedicated SQS event so ushnotice can update the
    # appointment cache payment_status in ushauth.
    _reason_str = (body.reason or "").strip().lower()
    _source_str = (body.source or "").strip().lower()
    if (
        payment_status == PaymentStatus.SUCCESS
        and _source_str in ("ushdesk", "desk", "ushspa app")
        and _reason_str == "paid on desk"
    ):
        try:
            await ushauth.update_appointment_cache_status_by_booking_id(
                booking_id=str(booking_id),
                new_status="confirmed",
                payment_status="success",
            )
            logger.info(
                "appointment_cache_updated_on_desk_payment_success",
                booking_id=str(booking_id),
                new_status="confirmed",
                payment_status="success",
            )
        except Exception as exc:
            logger.warning(
                "appointment_cache_update_on_desk_payment_failed",
                booking_id=str(booking_id),
                error=str(exc),
            )
        await booking_service._enqueue_event(
            BookingPaymentStatusSuccessEvent(
                booking_id=str(booking_id),
                customer_id=str(updated.customer_id),
                payment_status="success",
                source=body.source or "",
                reason=body.reason or "",
            )
        )

    return JSONResponse(
        content={
            "success": True,
            "data": _booking_to_detail(updated).model_dump(mode="json"),
        }
    )


class LinkBookingInvoiceRequest(BaseModel):
    invoice_number: str


@router.post(
    "/{booking_id}/invoice/",
    summary="Link or update invoice number on a booking",
    response_class=JSONResponse,
)
async def link_booking_invoice(
    booking_id: uuid.UUID,
    body: LinkBookingInvoiceRequest,
    _: RequireAppToken,
    session: DBSession,
) -> JSONResponse:
    """Set or update invoice_number on a booking and link it to any unlinked payments."""
    from app.booking.infrastructure.models import Booking

    b_res = await session.execute(select(Booking).where(Booking.id == booking_id))
    booking = b_res.scalar_one_or_none()
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking {booking_id} not found",
        )
    booking.invoice_number = body.invoice_number
    # Also update any payments for this booking that don't have an invoice_number
    await session.execute(
        update(Payment)
        .where(Payment.booking_id == booking.id)
        .values(invoice_number=body.invoice_number)
    )
    await session.commit()
    return JSONResponse(
        content={
            "success": True,
            "booking_id": str(booking_id),
            "invoice_number": body.invoice_number,
        }
    )

