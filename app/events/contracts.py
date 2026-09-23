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
        elif name == "Voucher.Active":
            return "voucher.active"
        elif name == "Voucher.PaymentPending":
            return "voucher.payment_pending"
        elif name == "Voucher.Redeemed":
            return "voucher.redeemed"
        elif name == "Gift.Purchase.Completed":
            return "gift.purchase.completed"
        elif name == "Gift.Claimed":
            return "gift.claimed"
        elif name == "Gift.Redeemed":
            return "gift.redeemed"
        elif name == "Gift.Delivered":
            return "gift.delivered"
        elif name == "Loyalty.Points.Credited":
            return "loyalty.points_credited"
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
    booking_number: str = ""
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
    booking_number: str = ""
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
    loyalty_points: int = 0
    arrangement_loyalty_points: int | None = None
    price_in_points: int = 0
    arrangement_price_in_points: int | None = None
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
    booking_number: str = ""
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
    loyalty_points: int = 0                          # Service-level earn points
    arrangement_loyalty_points: int | None = None    # Arrangement override (None = use service level)
    price_in_points: int = 0                         # Service-level redemption cost in points
    arrangement_price_in_points: int | None = None   # Arrangement redemption cost override
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
    booking_number: str = ""
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
    loyalty_points: int = 0
    arrangement_loyalty_points: int | None = None
    price_in_points: int = 0
    arrangement_price_in_points: int | None = None
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
    booking_number: str = ""
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
    # Loyalty fields — used by ushnotice to reverse points on cancellation
    is_eligible_for_loyalty: bool = False
    loyalty_points: int = 0
    arrangement_loyalty_points: int | None = None



@dataclass
class BookingCompletedEvent(BaseEvent):
    """Fired when a booking is marked as completed."""

    event_name: str = field(default="Booking.Completed", init=False)
    event_type: str = field(default="booking.completed", init=False)
    booking_id: str = ""
    booking_number: str = ""
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
    loyalty_points: int = 0
    arrangement_loyalty_points: int | None = None
    price_in_points: int = 0
    arrangement_price_in_points: int | None = None
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
    voucher_number: str = ""
    status: str = "active"
    gift_category: str = "service"
    ordered_items: list[dict[str, Any]] = field(default_factory=list)
    delivery_status: str | None = None
    delivery_address: dict[str, Any] = field(default_factory=dict)
    digital_product_data: dict[str, Any] = field(default_factory=dict)
    is_digital_gift_opened: bool = False
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
    gift_from: str | None = None
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
    voucher_number: str = ""
    status: str = "payment_pending"
    gift_category: str = "service"
    ordered_items: list[dict[str, Any]] = field(default_factory=list)
    delivery_status: str | None = None
    delivery_address: dict[str, Any] = field(default_factory=dict)
    digital_product_data: dict[str, Any] = field(default_factory=dict)
    is_digital_gift_opened: bool = False
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
    gift_from: str | None = None
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
    voucher_number: str = ""
    status: str = "redeemed"
    gift_category: str = "service"
    ordered_items: list[dict[str, Any]] = field(default_factory=list)
    delivery_status: str | None = None
    delivery_address: dict[str, Any] = field(default_factory=dict)
    digital_product_data: dict[str, Any] = field(default_factory=dict)
    is_digital_gift_opened: bool = False
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
    gift_from: str | None = None
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


# ── Shop Events ───────────────────────────────────────────────────────────────


@dataclass
class ShopOrderCreatedEvent(BaseEvent):
    """
    Fired when a new shop order is placed and payment is confirmed (payment_status = success).

    ushnotice handles this event to:
      1. Create a payment record in ushbooknpay (/api/v1/payments/).
      2. Send a WhatsApp/SMS confirmation with the tracking URL and tracking_code.
    """

    event_name: str = field(default="Shop.OrderCreated", init=False)
    event_type: str = field(default="shop.order_created", init=False)

    # ── Order identity ────────────────────────────────────────────────
    order_id: str = ""
    order_number: str = ""

    # ── Customer ──────────────────────────────────────────────────────
    customer_id: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    # Snapshot dict for payment record: {id, name, phone, email}
    customer_data: dict = field(default_factory=dict)

    # ── Delivery ──────────────────────────────────────────────────────
    delivery_address: str = ""

    # ── Tracking ──────────────────────────────────────────────────────
    # URL-safe token forming the public tracking URL path segment
    public_token: str = ""
    # 6-digit PIN sent to customer to confirm receipt
    tracking_code: str = ""

    # ── Financials ────────────────────────────────────────────────────
    subtotal: str = ""
    total_amount: str = ""
    currency: str = "KWD"

    # ── Payment classification ─────────────────────────────────────────
    # status at time of event (always "success" since we only fire on success)
    payment_status: str = "success"
    payment_method: str = ""       # card, knet, apple_pay, cash, etc.
    payment_type: str = ""         # gateway, desk, gift_voucher, etc.
    payment_provider: str = ""     # MyFatoorah, DirectLink, Other

    # ── Items snapshot ────────────────────────────────────────────────
    # List of dicts: {product_id, product_name, product_name_ar, quantity, unit_price, line_total}
    items: list = field(default_factory=list)

# ── Gift Purchase Events (V2) ─────────────────────────────────────────────────

@dataclass
class GiftPurchaseCompletedEvent(BaseEvent):
    """
    Fired when a Gift Voucher V2 purchase is successfully activated (payment confirmed).
    """
    event_name: str = field(default="Gift.Purchase.Completed", init=False)
    event_type: str = field(default="gift.purchase.completed", init=False)

    id: str = ""
    gift_type: str = ""  
    status: str = "ACTIVE"
    public_token: str = ""
    secret_code: str = ""  

    digital_gift_id: str | None = None
    digital_gift_data: dict[str, Any] = field(default_factory=dict)

    total_amount: str = ""
    currency: str = "KWD"
    expire_date: str | None = None

    gift_message: str | None = None
    gift_from: str | None = None
    gift_template: str | None = None

    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)

    recipient_phone: str | None = None
    recipient_id: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    recipient_language: str = "ar"  

    payment_id: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None

    created_at: str | None = None


@dataclass
class GiftClaimedEvent(BaseEvent):
    """
    Fired when a recipient successfully verifies the secret code (gift.claimed).
    """
    event_name: str = field(default="Gift.Claimed", init=False)
    event_type: str = field(default="gift.claimed", init=False)

    id: str = ""
    gift_type: str = ""
    public_token: str = ""
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    claimed_at: str | None = None


@dataclass
class GiftRedeemedPurchaseEvent(BaseEvent):
    """
    Fired when a Gift Purchase V2 is redeemed.
    """
    event_name: str = field(default="Gift.Redeemed", init=False)
    event_type: str = field(default="gift.redeemed", init=False)

    id: str = ""
    gift_type: str = ""
    public_token: str = ""
    total_amount: str = ""
    currency: str = "KWD"
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    recipient_language: str = "ar"
    redeemed_by: str | None = None
    booking_id: str | None = None
    redeemed_at: str | None = None


@dataclass
class GiftDeliveredEvent(BaseEvent):
    """
    Fired when a Physical Gift delivery status reaches DELIVERED.
    """
    event_name: str = field(default="Gift.Delivered", init=False)
    event_type: str = field(default="gift.delivered", init=False)

    id: str = ""
    gift_type: str = "PHYSICAL"
    public_token: str = ""
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    recipient_language: str = "ar"
    tracking_reference: str | None = None
    delivered_at: str | None = None


# ── Loyalty Events ────────────────────────────────────────────────────────────


@dataclass
class LoyaltyPointsCreditedEvent(BaseEvent):
    """
    Fired by ushbooknpay after loyalty points are successfully credited to a customer.

    Published after the internal credit API is called by ushnotice. Can be used
    by future consumers (push notifications, analytics, etc.).
    """

    event_name: str = field(default="Loyalty.Points.Credited", init=False)
    event_type: str = field(default="loyalty.points_credited", init=False)

    customer_id: str = ""
    booking_id: str = ""
    booking_number: str = ""
    points_credited: int = 0
    new_balance: int = 0
    points_expire_at: str = ""    # ISO datetime string
