"""
app/course/api/schemas.py
─────────────────────────
Request bodies and serializers of the course-subscription API (contract D).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.common.utils import to_local_tz
from app.course.domain import rules


class CreateCourseSubscriptionRequest(BaseModel):
    customer_id: uuid.UUID
    course_id: uuid.UUID
    discount: Decimal | None = None
    payment_option: Literal["pay_at_desk", "payment_link"] = "pay_at_desk"
    payment_gateway: Literal["myfatoorah", "tap", "deema"] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class ConfirmPaymentRequest(BaseModel):
    payment_provider: str
    payment_method: str | None = None
    payment_gateway: str | None = None
    transaction_reference: str | None = Field(default=None, max_length=255)
    payment_through: str | None = "ushdesk"


class PaymentLinkRequest(BaseModel):
    payment_gateway: Literal["myfatoorah", "tap", "deema"] | None = None


class CancelRequest(BaseModel):
    reason: str | None = None


class RefundRequest(BaseModel):
    reason: str = Field(min_length=1)
    refund_method: str | None = None


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    local = to_local_tz(dt)
    return local.isoformat() if local else None


def _str(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


def serialize_usage(usage: Any, booking_status: str | None = None) -> dict[str, Any]:
    return {
        "id": str(usage.id),
        "session_number": int(usage.session_number or 0),
        "booking_id": str(usage.booking_id),
        "booking_number": usage.booking_number,
        "status": usage.status,
        "booking_status": booking_status,
        "branch_id": _str(usage.branch_id),
        "branch_name": usage.branch_name,
        "therapist_id": _str(usage.therapist_id),
        "therapist_name": usage.therapist_name,
        "service_arrangement_id": _str(usage.service_arrangement_id),
        "service_arrangement_name": usage.service_arrangement_name,
        "service_name": usage.service_name,
        "appointment_start": _iso(usage.appointment_start),
        "appointment_end": _iso(usage.appointment_end),
        "reserved_at": _iso(usage.reserved_at),
        "consumed_at": _iso(usage.consumed_at),
        "released_at": _iso(usage.released_at),
        "release_reason": usage.release_reason,
    }


def serialize_subscription(sub: Any, sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    course = sub.course_data or {}
    eff = rules.effective_status(sub)
    data: dict[str, Any] = {
        "id": str(sub.id),
        "subscription_number": sub.subscription_number,
        "customer_id": str(sub.customer_id),
        "customer_data": sub.customer_data or {},
        "course_id": str(sub.course_id),
        "course_data": course,
        "course_code": course.get("code") or "",
        "course_name": course.get("name") or "",
        "service_id": str(sub.service_id),
        "service_name": course.get("service_name") or "",
        "arrangement_type": sub.arrangement_type,
        "total_sessions": int(sub.total_sessions or 0),
        "used_sessions": int(sub.used_sessions or 0),
        "reserved_sessions": int(sub.reserved_sessions or 0),
        "available_sessions": rules.available_sessions(sub),
        "price": f"{Decimal(str(sub.price or 0)):.3f}",
        "currency": sub.currency or "KWD",
        "status": sub.status,
        "effective_status": eff,
        "is_expired": eff == "expired",
        "is_redeemable": rules.is_redeemable(sub),
        "purchased_at": _iso(sub.purchased_at),
        "activated_at": _iso(sub.activated_at),
        "expires_at": _iso(sub.expires_at),
        "payment_id": _str(sub.payment_id),
        "payment_number": sub.payment_number,
        "payment_option": sub.payment_option,
        "payment_method": sub.payment_method,
        "payment_provider": sub.payment_provider,
        "payment_gateway": sub.payment_gateway,
        "payment_through": sub.payment_through,
        "payment_link_id": _str(sub.payment_link_id),
        "payment_url": sub.payment_url,
        "invoice_id": sub.invoice_id,
        "invoice_number": sub.invoice_number,
        "invoice_status": sub.invoice_status,
        "refund_id": _str(sub.refund_id),
        "refunded_at": _iso(sub.refunded_at),
        "refund_reason": sub.refund_reason,
        "sold_by_user": sub.sold_by_user,
        "sold_by_user_data": sub.sold_by_user_data or {},
        "notes": sub.notes,
        "created_at": _iso(sub.created_at),
        "updated_at": _iso(sub.updated_at),
    }
    if sessions is not None:
        data["sessions"] = sessions
    return data

