"""
app/booking/interfaces/schemas.py
──────────────────────────────────
Pydantic v2 request/response schemas for the Booking API.

Convention:
- *Request schemas: incoming payload validation
- *Response schemas: outgoing response serialization
- All UUIDs serialised as strings in responses
- All money as string with 3 decimal places (KWD convention)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.booking.domain.value_objects import BookingStatus, BookingType, PaymentStatus


# ── Shared sub-schemas ────────────────────────────────────────────────────────


class AddonSchema(BaseModel):
    addon_id: str
    name: str
    price: str


class PricingBreakdownSchema(BaseModel):
    arrangement_price: str
    price_for_extra_minutes: str = "0.000"
    addon_price: str
    discount: str
    tax: str
    fees: str
    total: str
    currency: str = "KWD"


class StatusHistoryItem(BaseModel):
    old_status: str | None
    new_status: str
    source: str | None
    reason: str | None
    created_at: datetime


# ── Request Schemas ───────────────────────────────────────────────────────────


class CreateBookingRequest(BaseModel):
    """POST /api/v1/bookings/"""

    # Customer override — if provided, customer data is fetched from ushauth
    # using this ID instead of the requesting user's own profile.
    customer_id: uuid.UUID | None = Field(
        default=None,
        alias="customerId",
        description=(
            "Optional: UUID of the customer to book for. "
            "If omitted, the authenticated user is the customer. "
            "If provided, customer data is fetched from ushauth."
        ),
    )

    # Service info
    service_id: uuid.UUID | None = Field(default=None, alias="serviceId")
    service_name: str | None = Field(default=None, alias="serviceName")
    service_category: str | None = Field(default=None, alias="serviceCategory")
    service_data: dict[str, Any] | None = Field(default=None, alias="serviceData")
    is_eligible_for_loyalty: bool = Field(
        default=False,
        alias="isEligibleForLoyalty",
        description="Whether this service participates in the loyalty programme",
    )
    base_price: str | Decimal | float | None = Field(default=None, alias="basePrice")
    base_duration: int | None = Field(default=None, alias="baseDuration")

    # Branch info
    branch_id: uuid.UUID | None = Field(default=None, alias="branchId")
    branch_name: str | None = Field(default=None, alias="branchName")
    branch_address: str | None = Field(default=None, alias="branchAddress")
    branch_data: dict[str, Any] | None = Field(default=None, alias="branchData")

    # Arrangement info
    service_arrangement_id: uuid.UUID | None = Field(default=None, alias="serviceArrangementId")
    arrangement_name: str | None = Field(default=None, alias="arrangementName")
    arrangement_type: str | None = Field(default=None, alias="arrangementType")
    room_name: str | None = Field(default=None, alias="roomName")
    service_arrangement_data: dict[str, Any] | None = Field(default=None, alias="serviceArrangementData")

    # Addons info
    selected_addons: list[dict[str, Any]] | list[Any] | None = Field(default_factory=list, alias="selectedAddons")
    addon_ids: list[str] = Field(default_factory=list)
    addons_price: str | Decimal | float | None = Field(default="0.00", alias="addonsPrice")
    addons_duration: int | None = Field(default=0, alias="addonsDuration")

    # Extra time info
    extra_minutes: int | None = Field(default=0, alias="extraMinutes")
    extra_price: str | Decimal | float | None = Field(default="0.00", alias="extraPrice")
    include_extra_minutes: bool = Field(default=False)

    # Therapists info
    therapist_id: uuid.UUID | None = Field(default=None, alias="therapistId")
    therapist_name: str | None = Field(default=None, alias="therapistName")
    therapist_data: dict[str, Any] | None = Field(default=None, alias="therapistData")
    selected_therapist_ids: list[uuid.UUID | str] | None = Field(default_factory=list, alias="selected_therapist_ids")
    therapist_ids: list[uuid.UUID | str] | None = Field(default_factory=list, alias="therapist_ids")

    # Date / Time info
    date: str | None = Field(default=None)
    appointment_date: str | None = Field(
        default=None,
        description="Appointment date in YYYY-MM-DD format. Accepts full ISO datetime strings too.",
    )
    formatted_date: str | None = Field(default=None, alias="formattedDate")
    time_slot: str | None = Field(default=None, alias="timeSlot")
    start_time: str | None = Field(
        default=None,
        description="Appointment start time in HH:MM or HH:MM:SS format. Alias for appointment_time.",
    )
    appointment_time: str | None = Field(
        default=None,
        description="Appointment time in HH:MM or HH:MM:SS format. Milliseconds and timezone suffixes are stripped.",
    )
    display_time: str | None = Field(default=None, alias="displayTime")
    appointment_start: datetime | None = Field(default=None)
    booking_type: BookingType | str | None = Field(default=BookingType.BRANCH, alias="bookingType")

    @field_validator("appointment_date", mode="before")
    @classmethod
    def sanitize_appointment_date(cls, v: Any) -> str | None:
        """Strip time/tz portion from date strings — accept YYYY-MM-DD or full ISO datetime."""
        if not v:
            return v
        if isinstance(v, (datetime, date)):
            return v.strftime("%Y-%m-%d")
        return str(v).split("T")[0].strip()

    @field_validator("appointment_time", mode="before")
    @classmethod
    def sanitize_appointment_time(cls, v: str | None) -> str | None:
        """Strip milliseconds and timezone suffix from time strings."""
        import re as _re
        if not v:
            return v
        s = str(v).strip()
        # Remove trailing Z
        if s.endswith("Z"):
            s = s[:-1]
        # Remove +HH:MM or -HH:MM offset
        s = _re.sub(r"[+-]\d{2}:\d{2}$", "", s)
        # Strip milliseconds
        s = s.split(".")[0]
        # Ensure HH:MM:SS
        return s if len(s.split(":")) == 3 else f"{s}:00"


    # Message / Notes
    customer_message: str | None = Field(default="", alias="customerMessage")
    customer_notes: str | None = Field(default=None, alias="customerNotes")

    # Pricing & Duration
    pricing_details: dict[str, Any] | None = Field(default=None, alias="pricingDetails")
    total_price: str | Decimal | float | None = Field(default=None, alias="totalPrice")
    total_duration: int | None = Field(default=None, alias="totalDuration")
    currency: str = Field(default="KWD")
    payments_meta: dict[str, Any] | None = Field(default=None, alias="paymentsMeta")

    # Loyalty booking fields (only sent when booking_type='loyalty')
    loyalty_data: dict[str, Any] | None = Field(
        default=None,
        alias="loyaltyData",
        description="Loyalty tracker and reward snapshot for loyalty-type bookings",
    )
    reward_id: uuid.UUID | None = Field(
        default=None,
        alias="rewardId",
        description="ID of the loyalty reward being redeemed (loyalty bookings only)",
    )

    # Gift voucher fields (only sent when the booking is paid via a gift voucher)
    voucher_id: uuid.UUID | None = Field(
        default=None,
        alias="voucherId",
        description="UUID of the gift voucher being used to pay for this booking.",
    )
    voucher_data: dict[str, Any] | None = Field(
        default=None,
        alias="voucherData",
        description="Snapshot of the gift voucher at redemption time.",
    )

    idempotency_key: str | None = Field(
        default=None, max_length=255, alias="Idempotency-Key"
    )


    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }


class UpdateBookingRequest(BaseModel):
    """
    PATCH /api/v1/bookings/{id}/ or PUT /api/v1/bookings/{id}/
    Partial or full update of a booking resource.
    """

    status: BookingStatus | None = Field(
        default=None,
        description="Target booking status: requested, payment_pending, confirmed, in_progress, completed, cancelled, reschedule_requested, reschedule_approved, no_show",
    )
    payment_status: PaymentStatus | None = Field(
        default=None,
        description="Payment status: not_initiated, initiated, pending, success, failed, cancelled, refunded, partially_refunded",
    )
    payments_meta: dict[str, Any] | None = Field(
        default=None,
        alias="paymentsMeta",
        description="Payment details snapshot and metadata",
    )
    therapist_id: uuid.UUID | None = Field(
        default=None,
        description="Reassign therapist (UUID)",
    )
    appointment_start: datetime | None = Field(
        default=None,
        description="New appointment start datetime",
    )
    extra_minutes: int | None = Field(
        default=None,
        ge=0,
        description="Update extra duration in minutes",
    )
    customer_notes: str | None = Field(
        default=None,
        max_length=1000,
        description="Notes from customer",
    )
    internal_notes: str | None = Field(
        default=None,
        description="Internal staff / admin notes",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Reason for change/update",
    )
    source: str = Field(
        default="admin",
        max_length=100,
        description="Actor or source making the update: admin, system, therapist, customer",
    )
    # Gift voucher fields — set when the booking is linked to a voucher redemption
    voucher_id: uuid.UUID | None = Field(
        default=None,
        alias="voucherId",
        description="UUID of the gift voucher used for this booking.",
    )
    voucher_data: dict[str, Any] | None = Field(
        default=None,
        alias="voucherData",
        description="Snapshot of the gift voucher at redemption time.",
    )

    @field_validator("appointment_start")
    @classmethod
    def validate_appointment_start_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("appointment_start must include timezone information.")
        return v


class CancelBookingRequest(BaseModel):
    """POST /api/v1/bookings/{id}/cancel/"""

    reason: str = Field(default="", max_length=500)


class UpdateBookingStatusRequest(BaseModel):
    """PATCH /api/v1/bookings/{id}/status/"""

    status: BookingStatus = Field(
        ...,
        description="Target booking status: requested, payment_pending, confirmed, in_progress, completed, cancelled, reschedule_requested, reschedule_approved, no_show",
    )
    payment_status: PaymentStatus | str | None = Field(
        default=None,
        description="Payment status: not_initiated, initiated, pending, success, failed, cancelled, refunded, partially_refunded",
    )
    payments_meta: dict[str, Any] | None = Field(
        default=None,
        alias="paymentsMeta",
        description="Payment details snapshot and metadata",
    )
    reason: str | None = Field(
        default=None, max_length=500, description="Reason for status change"
    )
    source: str = Field(
        default="admin",
        max_length=100,
        description="Actor making the status change: admin, system, therapist, customer",
    )
    # Loyalty booking fields (only required when booking_type='loyalty')
    loyalty_data: dict[str, Any] | None = Field(
        default=None,
        alias="loyaltyData",
        description="Loyalty tracker and reward snapshot for loyalty-type bookings",
    )
    reward_id: uuid.UUID | None = Field(
        default=None,
        alias="rewardId",
        description="ID of the loyalty reward being redeemed (loyalty bookings only)",
    )

    # Gift voucher fields — set when the status update involves a voucher redemption
    voucher_id: uuid.UUID | None = Field(
        default=None,
        alias="voucherId",
        description="UUID of the gift voucher used for this booking.",
    )
    voucher_data: dict[str, Any] | None = Field(
        default=None,
        alias="voucherData",
        description="Snapshot of the gift voucher at redemption time.",
    )

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }



class RescheduleRequest(BaseModel):
    """POST /api/v1/bookings/{id}/reschedule/"""

    new_start: datetime = Field(..., description="Requested new appointment start")

    @field_validator("new_start")
    @classmethod
    def validate_future_start(cls, v: datetime) -> datetime:
        from app.common.utils import utcnow

        if v.tzinfo is None:
            raise ValueError("new_start must include timezone information.")
        if v <= utcnow():
            raise ValueError("new_start must be in the future.")
        return v


# ── Response Schemas ──────────────────────────────────────────────────────────


class BookingListItem(BaseModel):
    """Lightweight booking item for list views."""

    id: str
    customer_id: str
    customer_data: dict[str, Any] | None = None
    branch_id: str | None = None
    branch_data: dict[str, Any] | None = None
    service_id: str
    service_data: dict[str, Any] | None = None
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] | None = None
    therapist_id: str
    therapist_data: dict[str, Any] | None = None
    appointment_date: str | None = None
    appointment_start: datetime
    appointment_end: datetime
    duration_minutes: int
    extra_minutes: int = 0
    total_duration: int | None = None
    addons_duration: int | None = None
    base_price: str | None = None
    booking_type: str = "branch"
    status: str
    payment_status: str
    payments_meta: dict[str, Any] | None = None
    total_amount: str
    currency: str
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] | None = None
    reward_id: str | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    created_at: datetime
    created_by: str | None = None

    @field_validator("appointment_date", mode="before")
    @classmethod
    def sanitize_appointment_date(cls, v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, (datetime, date)):
            return v.strftime("%Y-%m-%d")
        return str(v).split("T")[0].strip()


class BookingDetailResponse(BaseModel):
    """Full booking detail response."""

    id: str
    customer_id: str
    customer_data: dict[str, Any] | None = None
    branch_id: str | None = None
    branch_data: dict[str, Any] | None = None
    service_id: str
    service_data: dict[str, Any] | None = None
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] | None = None
    therapist_id: str
    therapist_data: dict[str, Any] | None = None
    appointment_date: str | None = None
    appointment_start: datetime
    appointment_end: datetime
    duration_minutes: int
    extra_minutes: int = 0
    total_duration: int | None = None
    addons_duration: int | None = None
    base_price: str | None = None
    price_for_extra_minutes: str = "0.000"
    booking_type: str = "branch"
    status: str
    payment_status: str
    payments_meta: dict[str, Any] | None = None
    pricing: PricingBreakdownSchema
    addons: list[AddonSchema]
    customer_notes: str | None = None
    internal_notes: str | None = None
    status_history: list[StatusHistoryItem] | None = None
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] | None = None
    reward_id: str | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None

    @field_validator("appointment_date", mode="before")
    @classmethod
    def sanitize_appointment_date(cls, v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, (datetime, date)):
            return v.strftime("%Y-%m-%d")
        return str(v).split("T")[0].strip()


class CreateBookingDataResponse(BaseModel):
    """Minimal response data for created booking."""

    booking_id: str
    customer_id: str
    final_amount: str
    status: str
    payment_status: str | None = None
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] | None = None
    reward_id: str | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None


class CreateBookingResponse(BaseModel):
    """Response to a successful booking creation."""

    success: bool = True
    data: CreateBookingDataResponse


class UpdateBookingResponse(BaseModel):
    """Response to a successful booking update."""

    success: bool = True
    data: BookingDetailResponse


class BookingListResponse(BaseModel):
    """Paginated list response."""

    success: bool = True
    data: list[BookingListItem]
    meta: dict[str, Any]
