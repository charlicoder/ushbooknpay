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

from app.common.utils import LocalDateTime
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


from app.booking.domain.services_snapshot import MAX_SERVICES_PER_BOOKING
from app.booking.domain.value_objects import (
    BookingPaymentGateway,
    BookingPaymentProvider,
    BookingPaymentThrough,
    BookingStatus,
    BookingType,
    PaymentStatus,
    PaymentType,
)


# ── Shared sub-schemas ────────────────────────────────────────────────────────


class AddonSchema(BaseModel):
    addon_id: str
    name: str
    price: str


_MAX_SNAPSHOT_BYTES = 32_768  # per snapshot dict (service_data / service_arrangement_data)


def _check_snapshot_size(value: dict[str, Any] | None, field: str) -> dict[str, Any] | None:
    """Reject oversized snapshot dicts to avoid JSONB bloat / abuse."""
    if value is None:
        return value
    if len(json.dumps(value, default=str)) > _MAX_SNAPSHOT_BYTES:
        raise ValueError(f"{field} is too large (max {_MAX_SNAPSHOT_BYTES} bytes).")
    return value


class BookingServiceItem(BaseModel):
    """
    One service inside a (multi-service) booking request.

    All services of a booking share the booking's branch and therapist.
    """

    service_id: uuid.UUID | None = Field(default=None, alias="serviceId")
    service_name: str | None = Field(default=None, alias="serviceName", max_length=255)
    service_category: str | None = Field(default=None, alias="serviceCategory", max_length=255)
    service_data: dict[str, Any] | None = Field(default=None, alias="serviceData")
    service_arrangement_id: uuid.UUID | None = Field(default=None, alias="serviceArrangementId")
    arrangement_name: str | None = Field(default=None, alias="arrangementName", max_length=255)
    arrangement_type: str | None = Field(default=None, alias="arrangementType", max_length=100)
    room_name: str | None = Field(default=None, alias="roomName", max_length=255)
    service_arrangement_data: dict[str, Any] | None = Field(
        default=None, alias="serviceArrangementData"
    )
    base_price: str | Decimal | float | None = Field(default=None, alias="basePrice")
    base_duration: int | None = Field(default=None, alias="baseDuration", ge=0, le=1440)
    is_eligible_for_loyalty: bool = Field(default=False, alias="isEligibleForLoyalty")
    loyalty_points: int = Field(default=0, alias="loyaltyPoints", ge=0)
    arrangement_loyalty_points: int | None = Field(
        default=None, alias="arrangementLoyaltyPoints", ge=0
    )
    price_in_points: int = Field(default=0, alias="priceInPoints", ge=0)
    arrangement_price_in_points: int | None = Field(
        default=None, alias="arrangementPriceInPoints", ge=0
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @field_validator("service_id", "service_arrangement_id", mode="before")
    @classmethod
    def _empty_uuid_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("service_data")
    @classmethod
    def _limit_service_data(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_snapshot_size(v, "service_data")

    @field_validator("service_arrangement_data")
    @classmethod
    def _limit_arrangement_data(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_snapshot_size(v, "service_arrangement_data")


class BookingServiceResponse(BaseModel):
    """One service entry in booking responses (mirrors ``bookings.services``)."""

    service_id: str
    service_data: dict[str, Any] | None = None
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] | None = None
    base_price: str | None = None
    duration_minutes: int = 0
    position: int = 0


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
    change_by_user: str | None = None
    change_by_user_data: dict[str, Any] | None = None
    reason: str | None
    created_at: LocalDateTime

    @field_validator("created_at")
    @classmethod
    def render_in_local_tz(cls, v: datetime) -> datetime:
        from app.common.utils import to_local_tz
        return to_local_tz(v)



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
    # Multi-service bookings: list of services sharing the same branch + therapist.
    # When omitted, the legacy flat fields below (service_id, service_arrangement_id,
    # service_data, ...) are wrapped into a single-item list for backward compatibility.
    services: list[BookingServiceItem] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SERVICES_PER_BOOKING,
        description=(
            "Services to book together (same branch and therapist). "
            "Each item carries service_id, service_arrangement_id, service_data, "
            "service_arrangement_data, etc."
        ),
    )
    service_id: uuid.UUID | None = Field(default=None, alias="serviceId")
    service_name: str | None = Field(default=None, alias="serviceName")
    service_category: str | None = Field(default=None, alias="serviceCategory")
    service_data: dict[str, Any] | None = Field(default=None, alias="serviceData")
    is_eligible_for_loyalty: bool = Field(
        default=False,
        alias="isEligibleForLoyalty",
        description="Whether this service participates in the loyalty programme",
    )
    loyalty_points: int = Field(
        default=0,
        alias="loyaltyPoints",
        ge=0,
        description="Points awarded to the customer after a confirmed booking (service-level).",
    )
    arrangement_loyalty_points: int | None = Field(
        default=None,
        alias="arrangementLoyaltyPoints",
        ge=0,
        description="Arrangement-specific override for loyalty points earned (takes precedence over loyalty_points).",
    )
    price_in_points: int = Field(
        default=0,
        alias="priceInPoints",
        ge=0,
        description="Points required to redeem this service for free (service-level).",
    )
    arrangement_price_in_points: int | None = Field(
        default=None,
        alias="arrangementPriceInPoints",
        ge=0,
        description="Arrangement-specific override for redemption cost in points.",
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
    # Second therapist for couple_room / double_room arrangements. Optional: when
    # omitted the server auto-assigns a free second therapist from availability.
    secondary_therapist_id: uuid.UUID | None = Field(default=None, alias="secondaryTherapistId")
    secondary_therapist_name: str | None = Field(default=None, alias="secondaryTherapistName")
    secondary_therapist_data: dict[str, Any] | None = Field(default=None, alias="secondaryTherapistData")
    # Opt-out for couple_room / double_room: when True the booking is created with
    # only one therapist (the second therapist was removed by the user).
    allow_single_therapist: bool = Field(default=False, alias="allowSingleTherapist")

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
    appointment_start: LocalDateTime | None = Field(default=None)
    booking_type: BookingType | str | None = Field(default=BookingType.BRANCH_SERVICE, alias="bookingType")
    payment_type: str | None = Field(
        default=None,
        alias="paymentType",
        description=(
            "How the booking is paid. One of: service (default), gift_voucher, rewarded. "
            "Auto-derived from booking_type if omitted."
        ),
    )
    status: BookingStatus | str | None = Field(
        default=None,
        description=(
            "Initial booking status: requested (default), draft, payment_pending, confirmed, etc. "
            "Case-insensitive."
        ),
    )
    payment_status: PaymentStatus | str | None = Field(
        default=None,
        alias="paymentStatus",
        description=(
            "Initial payment status: not_initiated (default), pending, success, etc. "
            "Case-insensitive; 'paid' is normalized to 'success'."
        ),
    )

    @field_validator("status", mode="before")
    @classmethod
    def sanitize_status(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, BookingStatus):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("checkedin", "checked-in"):
                s = "checked_in"
            elif s in ("inprogress", "in-progress"):
                s = "in_progress"
            if s in BookingStatus._value2member_map_:
                return BookingStatus(s)
            return s
        return v

    @field_validator("payment_status", mode="before")
    @classmethod
    def sanitize_payment_status(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, PaymentStatus):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s == "paid":
                return PaymentStatus.SUCCESS
            if s in PaymentStatus._value2member_map_:
                return PaymentStatus(s)
            return s
        return v

    @field_validator(
        "customer_id",
        "service_id",
        "branch_id",
        "service_arrangement_id",
        "therapist_id",
        "secondary_therapist_id",
        "reward_id",
        "voucher_id",
        mode="before",
    )
    @classmethod
    def coerce_empty_uuid_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

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
    discount: str | Decimal | float | None = Field(default=None, alias="discount")
    total_price: str | Decimal | float | None = Field(default=None, alias="totalPrice")
    total_duration: int | None = Field(default=None, alias="totalDuration")
    currency: str = Field(default="KWD")

    # Payment fields
    payment_id: str | None = Field(default=None, alias="paymentId")
    payment_provider: str | None = Field(
        default=None,
        alias="paymentProvider",
        description="Payment provider: MyFatoorah, PaymentLink, Deema, Other",
    )
    payment_gateway: str | None = Field(
        default=None,
        alias="paymentGateway",
        description="Payment gateway: KNET, TAP, Other",
    )
    payment_through: str | None = Field(
        default=None,
        alias="paymentThrough",
        description="Payment channel: ushspa, ushdesk, other",
    )
    payment_method: str | None = Field(default=None, alias="paymentMethod")
    payment_url: str | None = Field(default=None, alias="paymentUrl")
    payment_data: dict[str, Any] | None = Field(default=None, alias="paymentData")

    # Creator / User fields
    created_by_user: str | None = Field(
        default=None,
        alias="createdByUserId",
        description="User UUID who created the booking",
    )
    created_by: str | None = Field(
        default=None,
        alias="createdBy",
        description="Alias for created_by_user",
    )
    created_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="createdByUserData",
        description="Snapshot of user data (name, image, id, etc.) in json",
    )
    change_by_user: str | None = Field(
        default=None,
        alias="changeByUser",
        description="User UUID who initiated the creation/change",
    )
    change_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="changeByUserData",
        description="Snapshot of requesting user data in json",
    )

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

    # Invoice number from ushanr invoice record
    invoice_number: str | None = Field(
        default=None,
        alias="invoiceNumber",
        description="Invoice number from ushanr invoice record for this booking (e.g. INV/2026/10/00001).",
    )
    payment_number: str | None = Field(
        default=None,
        alias="paymentNumber",
        description="Payment number from payment record or voucher (e.g. PMT/2026/10/000001).",
    )

    idempotency_key: str | None = Field(
        default=None, max_length=255, alias="Idempotency-Key"
    )


    @field_validator("payment_provider", mode="before")
    @classmethod
    def sanitize_payment_provider(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentProvider.normalise(str(v)).value

    @field_validator("payment_gateway", mode="before")
    @classmethod
    def sanitize_payment_gateway(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentGateway.normalise(str(v)).value

    @field_validator("payment_through", mode="before")
    @classmethod
    def sanitize_payment_through(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentThrough.normalise(str(v)).value

    @model_validator(mode="after")
    def normalize_services(self) -> "CreateBookingRequest":
        """
        Keep ``services`` and the legacy flat single-service fields in sync.

        * ``services`` absent  → wrap the legacy flat fields into a 1-item list.
        * ``services`` present → validate it (ids required, no duplicates) and
          back-fill any empty legacy flat field from ``services[0]`` so code that
          still reads ``body.service_id`` etc. keeps working.
        """
        if not self.services:
            self.services = [
                BookingServiceItem(
                    service_id=self.service_id,
                    service_name=self.service_name,
                    service_category=self.service_category,
                    service_data=self.service_data,
                    service_arrangement_id=self.service_arrangement_id,
                    arrangement_name=self.arrangement_name,
                    arrangement_type=self.arrangement_type,
                    room_name=self.room_name,
                    service_arrangement_data=self.service_arrangement_data,
                    base_price=self.base_price,
                    base_duration=self.base_duration,
                    is_eligible_for_loyalty=self.is_eligible_for_loyalty,
                    loyalty_points=self.loyalty_points,
                    arrangement_loyalty_points=self.arrangement_loyalty_points,
                    price_in_points=self.price_in_points,
                    arrangement_price_in_points=self.arrangement_price_in_points,
                )
            ]
            return self

        seen: set[tuple[uuid.UUID, uuid.UUID | None]] = set()
        for idx, item in enumerate(self.services):
            if item.service_id is None:
                raise ValueError(f"services[{idx}].service_id is required.")
            key = (item.service_id, item.service_arrangement_id)
            if key in seen:
                raise ValueError(
                    f"services[{idx}] duplicates another service/arrangement pair."
                )
            seen.add(key)

        first = self.services[0]
        for flat, val in (
            ("service_id", first.service_id),
            ("service_name", first.service_name),
            ("service_category", first.service_category),
            ("service_data", first.service_data),
            ("service_arrangement_id", first.service_arrangement_id),
            ("arrangement_name", first.arrangement_name),
            ("arrangement_type", first.arrangement_type),
            ("room_name", first.room_name),
            ("service_arrangement_data", first.service_arrangement_data),
            ("base_price", first.base_price),
            ("base_duration", first.base_duration),
            ("arrangement_loyalty_points", first.arrangement_loyalty_points),
            ("arrangement_price_in_points", first.arrangement_price_in_points),
        ):
            if val is not None:
                setattr(self, flat, val)
        self.is_eligible_for_loyalty = first.is_eligible_for_loyalty
        self.loyalty_points = first.loyalty_points
        self.price_in_points = first.price_in_points
        return self

    @model_validator(mode="after")
    def sync_created_by_fields(self) -> "CreateBookingRequest":
        user_val = self.change_by_user or self.created_by_user or self.created_by
        if user_val:
            self.change_by_user = user_val
            self.created_by_user = user_val
            self.created_by = user_val
        data_val = self.change_by_user_data or self.created_by_user_data
        if data_val:
            self.change_by_user_data = data_val
            self.created_by_user_data = data_val
        return self

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
    payment_data: dict[str, Any] | None = Field(
        default=None,
        alias="paymentData",
        description="Payment details snapshot and metadata",
    )
    payment_type: str | None = Field(
        default=None,
        alias="paymentType",
        description="Payment type: service, gift_voucher, rewarded",
    )

    # Payment fields
    payment_id: str | None = Field(default=None, alias="paymentId")
    payment_provider: str | None = Field(
        default=None,
        alias="paymentProvider",
        description="Payment provider: MyFatoorah, PaymentLink, Deema, Other",
    )
    payment_gateway: str | None = Field(
        default=None,
        alias="paymentGateway",
        description="Payment gateway: KNET, TAP, Other",
    )
    payment_through: str | None = Field(
        default=None,
        alias="paymentThrough",
        description="Payment channel: ushspa, ushdesk, other",
    )
    payment_method: str | None = Field(default=None, alias="paymentMethod")
    payment_url: str | None = Field(default=None, alias="paymentUrl")

    # Creator / User fields
    created_by_user: str | None = Field(
        default=None,
        alias="createdByUserId",
        description="User UUID who created or updated the booking",
    )
    created_by: str | None = Field(
        default=None,
        alias="createdBy",
        description="Alias for created_by_user",
    )
    created_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="createdByUserData",
        description="Snapshot of user data (name, image, id, etc.) in json",
    )
    change_by_user: str | None = Field(
        default=None,
        alias="changeByUser",
        description="User UUID who initiated the update",
    )
    change_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="changeByUserData",
        description="Snapshot of user data in json",
    )

    therapist_id: uuid.UUID | None = Field(
        default=None,
        description="Reassign therapist (UUID)",
    )
    therapist_data: dict[str, Any] | None = Field(
        default=None,
        alias="therapistData",
        description="Snapshot of therapist data",
    )
    secondary_therapist_id: uuid.UUID | None = Field(
        default=None,
        alias="secondaryTherapistId",
        description="Reassign the second therapist (couple_room / double_room bookings)",
    )
    secondary_therapist_data: dict[str, Any] | None = Field(
        default=None,
        alias="secondaryTherapistData",
        description="Snapshot of the second therapist data",
    )
    branch_id: uuid.UUID | None = Field(
        default=None,
        alias="branchId",
        description="Updated branch ID",
    )
    branch_data: dict[str, Any] | None = Field(
        default=None,
        alias="branchData",
        description="Snapshot of updated branch data",
    )
    service_arrangement_id: uuid.UUID | None = Field(
        default=None,
        alias="serviceArrangementId",
        description="Updated room/arrangement ID (applies to the primary service, services[0])",
    )
    service_arrangement_data: dict[str, Any] | None = Field(
        default=None,
        alias="serviceArrangementData",
        description="Snapshot of updated room/arrangement data (applies to services[0])",
    )
    services: list[BookingServiceItem] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SERVICES_PER_BOOKING,
        description="Replace the full list of booked services (same branch and therapist).",
    )
    appointment_date: str | None = Field(
        default=None,
        alias="appointmentDate",
        description="Appointment date (YYYY-MM-DD)",
    )
    appointment_time: str | None = Field(
        default=None,
        alias="appointmentTime",
        description="Appointment time (HH:MM or HH:MM:SS)",
    )
    duration: int | None = Field(
        default=None,
        description="Service duration in minutes",
    )
    appointment_start: LocalDateTime | None = Field(
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
    invoice_number: str | None = Field(
        default=None,
        alias="invoiceNumber",
        description="Invoice number from ushanr invoice record for this booking.",
    )
    payment_number: str | None = Field(
        default=None,
        alias="paymentNumber",
        description="Payment number from payment record or voucher.",
    )
    addons: list[dict[str, Any]] | list[Any] | None = Field(
        default=None,
        description="Updated list of add-ons",
    )
    selected_addons: list[dict[str, Any]] | list[Any] | None = Field(
        default=None,
        alias="selectedAddons",
        description="Alias for addons",
    )
    addon_price: Decimal | str | float | None = Field(
        default=None,
        alias="addonPrice",
        description="Total addon price",
    )
    addons_price: Decimal | str | float | None = Field(
        default=None,
        alias="addonsPrice",
        description="Alias for addon_price",
    )
    addons_duration: int | None = Field(
        default=None,
        alias="addonsDuration",
        description="Total duration contributed by addons in minutes",
    )
    price_for_extra_minutes: Decimal | str | float | None = Field(
        default=None,
        alias="priceForExtraMinutes",
        description="Price for extra minutes",
    )
    extra_price: Decimal | str | float | None = Field(
        default=None,
        alias="extraPrice",
        description="Alias for price_for_extra_minutes",
    )
    total_duration: int | None = Field(
        default=None,
        alias="totalDuration",
        description="Total booked duration including add-on time and extra minutes",
    )
    total_amount: Decimal | str | float | None = Field(
        default=None,
        alias="totalAmount",
        description="Updated total booking amount",
    )
    total_price: Decimal | str | float | None = Field(
        default=None,
        alias="totalPrice",
        description="Alias for total_amount",
    )
    pricing_details: dict[str, Any] | None = Field(
        default=None,
        alias="pricingDetails",
        description="Pricing breakdown snapshot",
    )


    @field_validator("payment_provider", mode="before")
    @classmethod
    def sanitize_payment_provider(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentProvider.normalise(str(v)).value

    @field_validator("payment_gateway", mode="before")
    @classmethod
    def sanitize_payment_gateway(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentGateway.normalise(str(v)).value

    @field_validator("payment_through", mode="before")
    @classmethod
    def sanitize_payment_through(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentThrough.normalise(str(v)).value

    @model_validator(mode="after")
    def sync_created_by_fields(self) -> "UpdateBookingRequest":
        user_val = self.change_by_user or self.created_by_user or self.created_by
        if user_val:
            self.change_by_user = user_val
            self.created_by_user = user_val
            self.created_by = user_val
        data_val = self.change_by_user_data or self.created_by_user_data
        if data_val:
            self.change_by_user_data = data_val
            self.created_by_user_data = data_val
        return self

    @field_validator("therapist_id", "secondary_therapist_id", "voucher_id", "branch_id", "service_arrangement_id", mode="before")
    @classmethod
    def coerce_empty_uuid_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("appointment_start", mode="before")
    @classmethod
    def sanitize_appointment_start(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if " " in s and "T" not in s:
                s = s.replace(" ", "T")
            # Leave offset-less strings naive; they are interpreted as
            # Asia/Kuwait local time by the validator below.
            return s
        return v

    @field_validator("appointment_start")
    @classmethod
    def validate_appointment_start_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            from app.common.utils import to_local_tz
            return to_local_tz(v)
        return v

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }


class CancelBookingRequest(BaseModel):
    """POST /api/v1/bookings/{id}/cancel/"""

    reason: str = Field(default="", max_length=500)
    change_by_user: str | None = Field(
        default=None,
        alias="changeByUser",
        description="User UUID who cancelled the booking",
    )
    change_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="changeByUserData",
        description="Snapshot of user data who cancelled the booking",
    )

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }


class UpdateBookingStatusRequest(BaseModel):
    """PATCH /api/v1/bookings/{id}/status/"""

    status: BookingStatus = Field(
        ...,
        description="Target booking status: requested, payment_pending, confirmed, checked_in, in_progress, completed, cancelled, reschedule_requested, reschedule_approved, no_show",
    )
    payment_status: PaymentStatus | str | None = Field(
        default=None,
        description="Payment status: not_initiated, initiated, pending, success, failed, cancelled, refunded, partially_refunded",
    )
    payment_data: dict[str, Any] | None = Field(
        default=None,
        alias="paymentData",
        description="Payment details snapshot and metadata",
    )
    # ``payments_data`` (plural, snake_case) is the key sent by the mobile app.
    # We accept it here and promote its value into ``payment_data`` so the raw
    # gateway response dict is stored in the booking record unchanged.
    payments_data: dict[str, Any] | None = Field(
        default=None,
        description="Alias used by the mobile app for the payment gateway response (plural form). Merged into payment_data automatically.",
    )
    payment_type: str | None = Field(
        default=None,
        alias="paymentType",
        description="Payment type: service, gift_voucher, rewarded",
    )
    # Payment fields
    payment_id: str | None = Field(default=None, alias="paymentId")
    payment_provider: str | None = Field(
        default=None,
        alias="paymentProvider",
        description="Payment provider: MyFatoorah, PaymentLink, Deema, Other",
    )
    payment_gateway: str | None = Field(
        default=None,
        alias="paymentGateway",
        description="Payment gateway: KNET, TAP, Other",
    )
    payment_through: str | None = Field(
        default=None,
        alias="paymentThrough",
        description="Payment channel: ushspa, ushdesk, other",
    )
    payment_method: str | None = Field(default=None, alias="paymentMethod")
    payment_url: str | None = Field(default=None, alias="paymentUrl")
    invoice_number: str | None = Field(
        default=None,
        alias="invoiceNumber",
        description="Invoice number from ushanr invoice record for this booking.",
    )
    payment_number: str | None = Field(
        default=None,
        alias="paymentNumber",
        description="Payment number from payment record or voucher.",
    )


    @field_validator("status", mode="before")
    @classmethod
    def sanitize_status(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, BookingStatus):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("checkedin", "checked-in"):
                s = "checked_in"
            elif s in ("inprogress", "in-progress"):
                s = "in_progress"
            if s in BookingStatus._value2member_map_:
                return BookingStatus(s)
            return s
        return v

    @field_validator("payment_provider", mode="before")
    @classmethod
    def sanitize_payment_provider(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentProvider.normalise(str(v)).value

    @field_validator("payment_gateway", mode="before")
    @classmethod
    def sanitize_payment_gateway(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentGateway.normalise(str(v)).value

    @field_validator("payment_through", mode="before")
    @classmethod
    def sanitize_payment_through(cls, v: Any) -> str | None:
        if not v:
            return None
        return BookingPaymentThrough.normalise(str(v)).value
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
    change_by_user: str | None = Field(
        default=None,
        alias="changeByUser",
        description="User ID / sub who initiated the status change.",
    )
    changed_by: str | None = Field(
        default=None,
        alias="changedBy",
        description="Backward-compatible alias for change_by_user.",
    )
    change_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="changeByUserData",
        description="User details snapshot for who initiated the status change.",
    )

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }

    @model_validator(mode="after")
    def _promote_payments_data(self) -> "UpdateBookingStatusRequest":
        """Merge ``payments_data`` (mobile app key) into ``payment_data``.

        The mobile app sends the raw payment-gateway response under the key
        ``payments_data`` (plural).  If ``payment_data`` / ``paymentData`` is
        not also provided, we copy ``payments_data`` verbatim so the caller's
        intent is fulfilled without any transformation.
        """
        if self.payments_data and not self.payment_data:
            self.payment_data = self.payments_data
        return self



class RescheduleRequest(BaseModel):
    """POST /api/v1/bookings/{id}/reschedule/"""

    new_start: LocalDateTime = Field(..., description="Requested new appointment start")
    change_by_user: str | None = Field(
        default=None,
        alias="changeByUser",
        description="User UUID who requested reschedule",
    )
    change_by_user_data: dict[str, Any] | None = Field(
        default=None,
        alias="changeByUserData",
        description="Snapshot of user data who requested reschedule",
    )

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }

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
    booking_number: str | None = None
    customer_id: str
    customer_data: dict[str, Any] | None = None
    branch_id: str | None = None
    branch_data: dict[str, Any] | None = None
    service_id: str
    service_data: dict[str, Any] | None = None
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] | None = None
    services: list[BookingServiceResponse] = Field(default_factory=list)
    therapist_id: str
    therapist_data: dict[str, Any] | None = None
    # Second therapist for couple_room / double_room bookings (null otherwise)
    secondary_therapist_id: str | None = None
    secondary_therapist_data: dict[str, Any] | None = None
    therapist_ids: list[str] = Field(default_factory=list)
    appointment_date: str | None = None
    appointment_start: LocalDateTime
    appointment_end: LocalDateTime
    duration_minutes: int
    extra_minutes: int = 0
    total_duration: int | None = None
    addons_duration: int | None = None
    base_price: str | None = None
    booking_type: str = "branch_service"
    payment_type: str = "service"
    status: str
    payment_status: str
    payment_id: str | None = None
    payment_provider: str | None = None
    payment_gateway: str | None = None
    payment_through: str | None = None
    payment_method: str | None = None
    payment_url: str | None = None
    payment_data: dict[str, Any] | None = None
    total_amount: str
    total_price: str | None = None
    discount: str | None = None
    arrangement_price: str | None = None
    addon_price: str | None = None
    currency: str
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] | None = None
    reward_id: str | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    created_at: LocalDateTime
    created_by_user: str | None = None
    created_by_user_data: dict[str, Any] | None = None
    created_by: str | None = None
    invoice_number: str | None = None
    payment_number: str | None = None


    @model_validator(mode="after")
    def sync_created_by(self) -> "BookingListItem":
        if not self.created_by and self.created_by_user:
            self.created_by = self.created_by_user
        elif not self.created_by_user and self.created_by:
            self.created_by_user = self.created_by
        return self

    @field_validator("appointment_date", mode="before")
    @classmethod
    def sanitize_appointment_date(cls, v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, datetime):
            from app.common.utils import to_local_tz
            return to_local_tz(v).strftime("%Y-%m-%d")
        if isinstance(v, date):
            return v.strftime("%Y-%m-%d")
        return str(v).split("T")[0].strip()

    @field_validator("appointment_start", "appointment_end", "created_at")
    @classmethod
    def render_in_local_tz(cls, v: datetime) -> datetime:
        from app.common.utils import to_local_tz
        return to_local_tz(v)


class BookingDetailResponse(BaseModel):
    """Full booking detail response."""

    id: str
    booking_number: str | None = None
    customer_id: str
    customer_data: dict[str, Any] | None = None
    branch_id: str | None = None
    branch_data: dict[str, Any] | None = None
    service_id: str
    service_data: dict[str, Any] | None = None
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] | None = None
    services: list[BookingServiceResponse] = Field(default_factory=list)
    therapist_id: str
    therapist_data: dict[str, Any] | None = None
    # Second therapist for couple_room / double_room bookings (null otherwise)
    secondary_therapist_id: str | None = None
    secondary_therapist_data: dict[str, Any] | None = None
    therapist_ids: list[str] = Field(default_factory=list)
    appointment_date: str | None = None
    appointment_start: LocalDateTime
    appointment_end: LocalDateTime
    duration_minutes: int
    extra_minutes: int = 0
    total_duration: int | None = None
    addons_duration: int | None = None
    base_price: str | None = None
    price_for_extra_minutes: str = "0.000"
    booking_type: str = "branch_service"
    payment_type: str = "service"
    status: str
    payment_status: str
    payment_id: str | None = None
    payment_provider: str | None = None
    payment_gateway: str | None = None
    payment_through: str | None = None
    payment_method: str | None = None
    payment_url: str | None = None
    payment_data: dict[str, Any] | None = None
    pricing: PricingBreakdownSchema
    total_amount: str | None = None
    total_price: str | None = None
    discount: str | None = None
    arrangement_price: str | None = None
    addon_price: str | None = None
    currency: str = "KWD"
    addons: list[AddonSchema]
    customer_notes: str | None = None
    internal_notes: str | None = None
    status_history: list[StatusHistoryItem] | None = None
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] | None = None
    reward_id: str | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    created_at: LocalDateTime
    updated_at: LocalDateTime
    created_by_user: str | None = None
    created_by_user_data: dict[str, Any] | None = None
    created_by: str | None = None
    invoice_number: str | None = None
    invoice_id: str | None = None
    payment_number: str | None = None
    refund_number: str | None = None
    refund_id: str | None = None


    @model_validator(mode="after")
    def sync_created_by(self) -> "BookingDetailResponse":
        if not self.created_by and self.created_by_user:
            self.created_by = self.created_by_user
        elif not self.created_by_user and self.created_by:
            self.created_by_user = self.created_by
        return self

    @field_validator("appointment_date", mode="before")
    @classmethod
    def sanitize_appointment_date(cls, v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, datetime):
            from app.common.utils import to_local_tz
            return to_local_tz(v).strftime("%Y-%m-%d")
        if isinstance(v, date):
            return v.strftime("%Y-%m-%d")
        return str(v).split("T")[0].strip()

    @field_validator("appointment_start", "appointment_end", "created_at", "updated_at")
    @classmethod
    def render_in_local_tz(cls, v: datetime) -> datetime:
        from app.common.utils import to_local_tz
        return to_local_tz(v)


class CreateBookingDataResponse(BaseModel):
    """Minimal response data for created booking."""

    booking_id: str
    booking_number: str | None = None
    customer_id: str
    services: list[BookingServiceResponse] = Field(default_factory=list)
    final_amount: str
    total_amount: str | None = None
    total_price: str | None = None
    discount: str | None = None
    arrangement_price: str | None = None
    addon_price: str | None = None
    currency: str = "KWD"
    pricing: PricingBreakdownSchema | None = None
    status: str
    payment_status: str | None = None
    payment_id: str | None = None
    payment_provider: str | None = None
    payment_gateway: str | None = None
    payment_through: str | None = None
    payment_method: str | None = None
    payment_url: str | None = None
    payment_type: str | None = None
    payment_data: dict[str, Any] | None = None
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] | None = None
    reward_id: str | None = None
    voucher_id: str | None = None
    voucher_data: dict[str, Any] | None = None
    created_by_user: str | None = None
    created_by_user_data: dict[str, Any] | None = None
    created_by: str | None = None
    invoice_number: str | None = None
    invoice_id: str | None = None
    payment_number: str | None = None

    @model_validator(mode="after")
    def sync_created_by(self) -> "CreateBookingDataResponse":
        if not self.created_by and self.created_by_user:
            self.created_by = self.created_by_user
        elif not self.created_by_user and self.created_by:
            self.created_by_user = self.created_by
        return self


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
