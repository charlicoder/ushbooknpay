"""
app/events/contracts.py
────────────────────────
Domain event contracts (payload schemas) for SQS messages.

All events are plain dataclasses — no framework dependencies.
Events are serialised to JSON for SQS.

Naming convention:
- event_name: <Domain>.<Entity>.<Action> (e.g. Booking.Created, Booking.Requested, Booking.Confirmed)
- event_type: <domain>.<action> (e.g. booking.created, booking.requested, booking.confirmed)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _now_iso() -> str:
    from app.common.utils import utcnow
    return utcnow().isoformat()


def _new_event_id() -> str:
    return str(uuid.uuid4())


@dataclass
class BaseEvent:
    """Root event with standard metadata fields."""

    event_id: str = field(default_factory=_new_event_id)
    occurred_at: str = field(default_factory=_now_iso)
    schema_version: str = "1.0"
    source: str = "ushbooknpay"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if "event_type" not in data or not data["event_type"]:
            data["event_type"] = getattr(self, "event_type", getattr(self, "event_name", "").lower())
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @property
    def event_name(self) -> str:
        raise NotImplementedError

    @property
    def event_type(self) -> str:
        name = getattr(self, "event_name", "")
        if name == "Booking.Requested":
            return "booking.requested"
        elif name == "Booking.Created":
            return "booking.created"
        elif name == "Booking.Confirmed":
            return "booking.confirmed"
        elif name == "Booking.PaymentPending":
            return "booking.payment_pending"
        elif name == "Booking.Completed":
            return "booking.completed"
        elif name == "Booking.Loyalty":
            return "bookings.loyalty"
        elif name == "Loyalty.Rewarded":
            return "loyalty.rewarded"
        elif name == "Loyalty.Redeemed":
            return "loyalty.redeedmed"
        elif name == "Voucher.Active":
            return "voucher.active"
        elif name == "Voucher.PaymentPending":
            return "voucher.payment_pending"
        elif name == "Voucher.Redeemed":
            return "voucher.redeemed"
        return name.lower().replace(".", "_")


# ── Booking Events ────────────────────────────────────────────────────────────


@dataclass
class BookingRequestedEvent(BaseEvent):
    """Fired when a new booking is requested."""

    event_name: str = field(default="Booking.Requested", init=False)
    event_type: str = field(default="booking.requested", init=False)
    booking_id: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    branch_id: str = ""
    branch_name: str = ""
    service_id: str = ""
    service_name: str = ""
    therapist_id: str = ""
    therapist_name: str = ""
    appointment_start: str = ""  # ISO datetime
    appointment_end: str = ""
    duration_minutes: int = 0
    total_amount: str = ""
    currency: str = "KWD"
    status: str = "requested"


@dataclass
class BookingCreatedEvent(BaseEvent):
    """Fired when a new booking is created (status=REQUESTED)."""

    event_name: str = field(default="Booking.Created", init=False)
    event_type: str = field(default="booking.created", init=False)
    booking_id: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    branch_id: str = ""
    branch_name: str = ""
    service_id: str = ""
    service_name: str = ""
    therapist_id: str = ""
    therapist_name: str = ""
    appointment_start: str = ""  # ISO datetime
    appointment_end: str = ""
    duration_minutes: int = 0
    total_amount: str = ""
    currency: str = "KWD"
    status: str = "requested"


@dataclass
class BookingStatusUpdatedEvent(BaseEvent):
    """Fired when booking status is updated."""

    event_name: str = field(default="Booking.StatusUpdated", init=False)
    event_type: str = field(default="booking.status_updated", init=False)
    booking_id: str = ""
    booking_reference: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    customer_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str = ""
    branch_name: str = ""
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_name: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str = ""
    service_arrangement_name: str = ""
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    therapist_id: str = ""
    therapist_name: str = ""
    therapist_data: dict[str, Any] = field(default_factory=dict)
    appointment_start: str = ""
    appointment_end: str = ""
    appointment_date: str = ""
    appointment_starttime: str = ""
    appointment_endtime: str = ""
    appointment_time: str = ""
    duration_minutes: int = 0
    extra_minutes: int = 0
    total_duration: int = 0
    addons_duration: int = 0
    booking_type: str = "branch_service"
    payment_type: str = "service"
    status: str = ""
    payment_status: str = ""
    total_amount: str = ""
    currency: str = "KWD"
    pricing: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    customer_notes: str | None = None
    internal_notes: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] = field(default_factory=dict)
    reward_id: str = ""
    voucher_id: str = ""
    voucher_data: dict[str, Any] = field(default_factory=dict)
    old_status: str | None = None
    new_status: str = ""
    reason: str | None = None
    source: str = "api"
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass
class BookingConfirmedEvent(BaseEvent):
    """Fired when payment succeeds and booking is confirmed."""

    event_name: str = field(default="Booking.Confirmed", init=False)
    event_type: str = field(default="booking.confirmed", init=False)
    booking_id: str = ""
    booking_reference: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    customer_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str = ""
    branch_name: str = ""
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_name: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str = ""
    service_arrangement_name: str = ""
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    therapist_id: str = ""
    therapist_name: str = ""
    therapist_data: dict[str, Any] = field(default_factory=dict)
    appointment_start: str = ""
    appointment_end: str = ""
    appointment_date: str = ""       # e.g. "2026-08-25"
    appointment_starttime: str = ""  # e.g. "09:00"
    appointment_endtime: str = ""    # e.g. "10:00"
    appointment_time: str = ""
    duration_minutes: int = 0
    extra_minutes: int = 0
    total_duration: int = 0
    addons_duration: int = 0
    booking_type: str = "branch_service"
    payment_type: str = "service"
    status: str = "confirmed"
    payment_status: str = "paid"
    total_amount: str = ""
    currency: str = "KWD"
    pricing: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    customer_notes: str | None = None
    internal_notes: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] = field(default_factory=dict)
    reward_id: str = ""
    voucher_id: str = ""
    voucher_data: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass
class BookingPaymentPendingEvent(BaseEvent):
    """Fired when booking status is payment_pending."""

    event_name: str = field(default="Booking.PaymentPending", init=False)
    event_type: str = field(default="booking.payment_pending", init=False)
    booking_id: str = ""
    booking_reference: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    customer_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str = ""
    branch_name: str = ""
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_name: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str = ""
    service_arrangement_name: str = ""
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    therapist_id: str = ""
    therapist_name: str = ""
    therapist_data: dict[str, Any] = field(default_factory=dict)
    appointment_start: str = ""
    appointment_end: str = ""
    appointment_date: str = ""
    appointment_starttime: str = ""
    appointment_endtime: str = ""
    appointment_time: str = ""
    duration_minutes: int = 0
    extra_minutes: int = 0
    total_duration: int = 0
    addons_duration: int = 0
    booking_type: str = "branch_service"
    payment_type: str = "service"
    status: str = "payment_pending"
    payment_status: str = "pending"
    total_amount: str = ""
    currency: str = "KWD"
    pricing: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    customer_notes: str | None = None
    internal_notes: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] = field(default_factory=dict)
    reward_id: str = ""
    voucher_id: str = ""
    voucher_data: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass
class BookingCancelledEvent(BaseEvent):
    """Fired when a booking is cancelled."""

    event_name: str = field(default="Booking.Cancelled", init=False)
    event_type: str = field(default="booking.cancelled", init=False)
    booking_id: str = ""
    customer_id: str = ""
    branch_id: str = ""
    service_id: str = ""
    service_arrangement_id: str = ""
    therapist_id: str = ""
    # Full ISO datetimes kept for backward compatibility
    appointment_start: str = ""
    appointment_end: str = ""
    # Extracted date/time components
    appointment_date: str = ""
    appointment_starttime: str = ""
    appointment_endtime: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    cancellation_reason: str = ""
    refund_issued: bool = False
    refund_amount: str | None = None


@dataclass
class BookingCompletedEvent(BaseEvent):
    """Fired when a booking is marked as completed."""

    event_name: str = field(default="Booking.Completed", init=False)
    event_type: str = field(default="booking.completed", init=False)
    booking_id: str = ""
    booking_reference: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    customer_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str = ""
    branch_name: str = ""
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_name: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str = ""
    service_arrangement_name: str = ""
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    therapist_id: str = ""
    therapist_name: str = ""
    therapist_data: dict[str, Any] = field(default_factory=dict)
    appointment_start: str = ""
    appointment_end: str = ""
    appointment_date: str = ""
    appointment_starttime: str = ""
    appointment_endtime: str = ""
    appointment_time: str = ""
    duration_minutes: int = 0
    extra_minutes: int = 0
    total_duration: int = 0
    addons_duration: int = 0
    booking_type: str = "branch_service"
    payment_type: str = "service"
    status: str = "completed"
    payment_status: str = ""
    total_amount: str = ""
    currency: str = "KWD"
    pricing: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    customer_notes: str | None = None
    internal_notes: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    is_eligible_for_loyalty: bool = False
    loyalty_data: dict[str, Any] = field(default_factory=dict)
    reward_id: str = ""
    voucher_id: str = ""
    voucher_data: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass
class BookingNoShowEvent(BaseEvent):
    """Fired when a customer does not show up."""

    event_name: str = field(default="Booking.NoShow", init=False)
    event_type: str = field(default="booking.no_show", init=False)
    booking_id: str = ""
    customer_id: str = ""
    therapist_id: str | None = None


@dataclass
class RescheduleRequestedEvent(BaseEvent):
    """Fired when a customer requests a reschedule."""

    event_name: str = field(default="Booking.RescheduleRequested", init=False)
    event_type: str = field(default="booking.reschedule_request", init=False)
    booking_id: str = ""
    customer_id: str = ""
    current_appointment_start: str = ""
    requested_appointment_start: str = ""
    requested_appointment_end: str = ""


@dataclass
class BookingPaymentStatusSuccessEvent(BaseEvent):
    """Fired when booking payment_status is updated to 'success' via a desk payment.

    Triggered when PATCH /bookings/{id}/status/ is called with:
      - payment_status = 'success'
      - source = 'ushspa app'
      - reason = 'Paid on desk'

    Downstream consumers (ushnotice) use this to update the
    appointment cache payment_status in ushauth.
    """

    event_name: str = field(default="Booking.PaymentStatusSuccess", init=False)
    event_type: str = field(default="booking.updated_payment_status_to_success", init=False)
    booking_id: str = ""
    customer_id: str = ""
    payment_status: str = "success"
    source: str = ""
    reason: str = ""


# ── Payment Events ────────────────────────────────────────────────────────────


@dataclass
class PaymentInitiatedEvent(BaseEvent):
    """Fired when a payment session is created."""

    event_name: str = field(default="Payment.Initiated", init=False)
    event_type: str = field(default="payment.initiated", init=False)
    payment_id: str = ""
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    customer_id: str = ""
    provider: str = ""
    amount: str = ""
    currency: str = "KWD"


@dataclass
class PaymentSucceededEvent(BaseEvent):
    """Fired when a payment is confirmed by the provider."""

    event_name: str = field(default="Payment.Succeeded", init=False)
    event_type: str = field(default="payment.success", init=False)
    payment_id: str = ""
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    customer_id: str = ""
    provider: str = ""
    provider_reference: str = ""
    amount: str = ""
    currency: str = "KWD"
    payment_method: str = ""


@dataclass
class PaymentFailedEvent(BaseEvent):
    """Fired when a payment fails or is rejected by the provider."""

    event_name: str = field(default="Payment.Failed", init=False)
    event_type: str = field(default="payment.failed", init=False)
    payment_id: str = ""
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    customer_id: str = ""
    provider: str = ""
    failure_reason: str = ""


@dataclass
class RefundIssuedEvent(BaseEvent):
    """Fired when a refund is successfully issued."""

    event_name: str = field(default="Payment.RefundIssued", init=False)
    event_type: str = field(default="payment.refunded", init=False)
    payment_id: str = ""
    booking_id: str | None = None
    voucher_id: str | None = None
    payment_for: str = "service"
    customer_id: str = ""
    provider: str = ""
    refund_amount: str = ""
    currency: str = "KWD"


# ── Loyalty Events ────────────────────────────────────────────────────────────


@dataclass
class LoyaltyRewardedEvent(BaseEvent):
    """
    Fired when a customer earns a loyalty reward (every N confirmed branch bookings).

    Published by ushbooknpay immediately after the LoyaltyReward record is created
    and the booking_count resets to zero.

    Consumers (e.g. ushnotice) use this event to send reward notifications
    via Email, SMS, or WhatsApp.
    """

    event_name: str = field(default="Loyalty.Rewarded", init=False)
    event_type: str = field(default="loyalty.rewarded", init=False)

    # Reward + tracker identifiers
    reward_id: str = ""
    tracker_id: str = ""

    # Customer context — passed in from the caller when available
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""

    # Service context
    service_id: str = ""
    service_name: str = ""
    service_arrangement_id: str = ""

    # Loyalty program metadata
    bookings_required: int = 5
    total_rewards_earned: int = 0

    # Reward lifecycle
    reward_status: str = "available"
    expires_at: str = ""  # ISO datetime

    # Booking that triggered the reward (the Nth booking)
    booking_id: str = ""


@dataclass
class BookingLoyaltyEvent(BaseEvent):
    """
    Fired when a loyalty-type booking is confirmed with payment_status=rewarded.

    Triggered on both POST /api/v1/bookings/ and PATCH /api/v1/bookings/<id>/status/
    whenever: booking_type == 'loyalty' AND status == 'confirmed' AND payment_status == 'rewarded'.

    Carries the full booking snapshot plus the loyalty/reward data so downstream
    consumers (ushnotice, analytics, etc.) can act on the redeemed reward.
    """

    event_name: str = field(default="Booking.Loyalty", init=False)
    event_type: str = field(default="bookings.loyalty", init=False)

    # ── Core booking fields ───────────────────────────────────────────
    booking_id: str = ""
    booking_reference: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    customer_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str = ""
    branch_name: str = ""
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_name: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str = ""
    service_arrangement_name: str = ""
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    therapist_id: str = ""
    therapist_name: str = ""
    therapist_data: dict[str, Any] = field(default_factory=dict)
    appointment_start: str = ""
    appointment_end: str = ""
    appointment_date: str = ""
    appointment_starttime: str = ""
    appointment_endtime: str = ""
    appointment_time: str = ""
    duration_minutes: int = 0
    extra_minutes: int = 0
    total_duration: int = 0
    addons_duration: int = 0
    booking_type: str = "loyalty"
    payment_type: str = "service"
    status: str = "confirmed"
    payment_status: str = "rewarded"
    total_amount: str = ""
    currency: str = "KWD"
    pricing: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    customer_notes: str | None = None
    internal_notes: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    is_eligible_for_loyalty: bool = True
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""

    # ── Loyalty / reward snapshot ─────────────────────────────────────
    reward_id: str = ""
    loyalty_data: dict[str, Any] = field(default_factory=dict)
    voucher_id: str = ""
    voucher_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoyaltyRedeemedEvent(BaseEvent):
    """
    Fired when a customer redeems a loyalty reward.

    Published after POST /api/v1/promotions/loyalty/rewards/<reward_id>/redeem/
    with event_type="loyalty.redeedmed".
    """

    event_name: str = field(default="Loyalty.Redeemed", init=False)
    event_type: str = field(default="loyalty.redeedmed", init=False)

    # Full reward dictionary matching the redeem response (no nested 'data' duplication)
    reward: dict[str, Any] = field(default_factory=dict)

    # Flattened reward fields
    id: str = ""
    reward_id: str = ""
    customer_id: str = ""
    service_id: str = ""
    service_arrangement_id: str | None = None
    service_name: str = ""
    status: str = "redeemed"
    earned_from_booking_id: str | None = None
    redeemed_in_booking_id: str | None = None
    redeemed_at: str = ""
    expires_at: str = ""
    created_at: str = ""

    # Booking context — populated from the linked booking at redemption time
    therapist_id: str = ""
    appointment_date: str = ""
    appointment_time: str = ""
    duration: int = 0


# ── Gift Voucher Events ───────────────────────────────────────────────────────


@dataclass
class VoucherActiveEvent(BaseEvent):
    """
    Fired when a gift voucher transitions to 'active' status.

    Payment has been confirmed. ushnotice uses this event to send the
    secret_code to the recipient via Email/SMS/WhatsApp.
    """

    event_name: str = field(default="Voucher.Active", init=False)
    event_type: str = field(default="voucher.active", init=False)

    # ── Voucher identity ──────────────────────────────────────────────
    id: str = ""
    status: str = "active"
    public_token: str = ""
    secret_code: str = ""  # 6-digit code to be delivered to recipient

    # ── Service context ───────────────────────────────────────────────
    service_id: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str | None = None
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    extra_time: int = 0
    price_for_extra_time: str | None = None  # Serialised Decimal — e.g. "5.000"


    # ── Financial ─────────────────────────────────────────────────────
    total_duration: int = 0
    total_amount: str = ""
    currency: str = "KWD"

    # ── Personalisation ───────────────────────────────────────────────
    gift_message: str | None = None
    gift_template: str | None = None
    expire_date: str | None = None

    # ── Sender ────────────────────────────────────────────────────────
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)

    # ── Recipient ─────────────────────────────────────────────────────
    recipient_phone: str | None = None
    recipient_id: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)

    # ── Payment & timestamps ──────────────────────────────────────────
    # payment_id is a gateway reference string (e.g. "100624710000000255"), not UUID
    payment_id: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    payment_url: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None
    booking_id: str | None = None
    booking_data: dict[str, Any] = field(default_factory=dict)
    redeemed_booking_id: str | None = None
    redeemed_at: str | None = None
    redeemed_by: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class VoucherPaymentPendingEvent(BaseEvent):
    """
    Fired when a gift voucher transitions to 'payment_pending' status.

    Payment has been initiated but not yet confirmed. ushnotice may send
    a payment-pending reminder to the sender.
    """

    event_name: str = field(default="Voucher.PaymentPending", init=False)
    event_type: str = field(default="voucher.payment_pending", init=False)

    # ── Voucher identity ──────────────────────────────────────────────
    id: str = ""
    status: str = "payment_pending"
    public_token: str = ""
    secret_code: str = ""  # Accepted from snapshot but NOT delivered at this stage


    # ── Service context ───────────────────────────────────────────────
    service_id: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str | None = None
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    extra_time: int = 0
    price_for_extra_time: str | None = None  # Serialised Decimal — e.g. "5.000"

    # ── Financial ─────────────────────────────────────────────────────
    total_duration: int = 0
    total_amount: str = ""
    currency: str = "KWD"

    # ── Personalisation ───────────────────────────────────────────────
    gift_message: str | None = None
    gift_template: str | None = None
    expire_date: str | None = None

    # ── Sender ────────────────────────────────────────────────────────
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)

    # ── Recipient ─────────────────────────────────────────────────────
    recipient_phone: str | None = None
    recipient_id: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)

    # ── Payment & timestamps ──────────────────────────────────────────
    # payment_id is a gateway reference string (e.g. "100624710000000255"), not UUID
    payment_id: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    payment_url: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None
    booking_id: str | None = None
    booking_data: dict[str, Any] = field(default_factory=dict)
    redeemed_booking_id: str | None = None
    redeemed_at: str | None = None
    redeemed_by: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    # Note: secret_code intentionally omitted — not yet delivered at this stage


@dataclass
class VoucherRedeemedEvent(BaseEvent):
    """
    Fired when a gift voucher transitions to 'redeemed' status.

    The recipient has used the voucher to make a booking. ushnotice may
    send confirmation messages to both sender and recipient.
    """

    event_name: str = field(default="Voucher.Redeemed", init=False)
    event_type: str = field(default="voucher.redeemed", init=False)

    # ── Voucher identity ──────────────────────────────────────────────
    id: str = ""
    status: str = "redeemed"
    public_token: str = ""
    secret_code: str = ""  # Accepted from snapshot but NOT delivered at redemption stage

    # ── Service context ───────────────────────────────────────────────
    service_id: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    branch_id: str | None = None
    branch_data: dict[str, Any] = field(default_factory=dict)
    service_arrangement_id: str | None = None
    service_arrangement_data: dict[str, Any] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    extra_time: int = 0
    price_for_extra_time: str | None = None  # Serialised Decimal — e.g. "5.000"

    # ── Financial ─────────────────────────────────────────────────────
    total_duration: int = 0
    total_amount: str = ""
    currency: str = "KWD"

    # ── Personalisation ───────────────────────────────────────────────
    gift_message: str | None = None
    gift_template: str | None = None
    expire_date: str | None = None

    # ── Sender ────────────────────────────────────────────────────────
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)

    # ── Recipient ─────────────────────────────────────────────────────
    recipient_phone: str | None = None
    recipient_id: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)

    # ── Redemption ────────────────────────────────────────────────────
    redeemed_booking_id: str | None = None
    redeemed_at: str | None = None
    redeemed_by: str | None = None

    # ── Payment & timestamps ──────────────────────────────────────────
    # payment_id is a gateway reference string (e.g. "100624710000000255"), not UUID
    payment_id: str | None = None
    payment_data: dict[str, Any] = field(default_factory=dict)
    payment_url: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None
    booking_id: str | None = None
    booking_data: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    # Note: secret_code intentionally omitted from redemption event

