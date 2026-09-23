"""
app/voucher/interfaces/schemas.py
───────────────────────────────────
Pydantic v2 request/response schemas for the Gift Voucher API.

Design decisions:
  - secret_code is NEVER included in public-facing response schemas.
    It is only exposed in the detail response when the requester is the
    sender or an admin (filtered at the router level).
  - public_token is always included so the shareable URL can be built.
  - All UUID fields accept uuid.UUID for type safety.
  - Monetary amounts are returned as strings to preserve KWD 3-decimal precision.
  - payment_id is a plain str (gateway reference such as "100624710000000255"),
    not a UUID. payment_data stores the full provider response snapshot (JSONB).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.voucher.domain.value_objects import (
    DeliveryStatus,
    GiftCategory,
    GiftVoucherStatus,
    VoucherPaymentProvider,
    VoucherPaymentThrough,
)


# ── Nested sub-schemas ────────────────────────────────────────────────────────


class SenderDetails(BaseModel):
    name: str = Field(default="", description="Sender's display name.")
    phone_number: str = Field(default="", description="Sender's phone number.")

    model_config = {"extra": "allow"}


class RecipientDetails(BaseModel):
    name: str = Field(default="", description="Recipient's display name.")
    email: str = Field(default="", description="Recipient's email address.")
    phone_number: str = Field(default="", description="Recipient's phone number.")

    model_config = {"extra": "allow"}


# ── Request schemas ───────────────────────────────────────────────────────────


class CreateGiftVoucherRequest(BaseModel):
    """POST /api/v1/vouchers/ — create a new gift voucher."""

    # ── Service & branch ──────────────────────────────────────────────
    service_id: uuid.UUID | None = Field(
        default=None, description="Optional UUID of the service being gifted."
    )
    service_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Snapshot of service metadata (name, category, etc.).",
    )
    branch_id: uuid.UUID | None = Field(
        default=None, description="UUID of the branch where the service will be rendered."
    )
    branch_data: dict[str, Any] = Field(
        default_factory=dict, description="Snapshot of branch metadata."
    )
    service_arrangement_id: uuid.UUID | None = Field(
        default=None, description="UUID of the service arrangement (room/package)."
    )
    service_arrangement_data: dict[str, Any] = Field(
        default_factory=dict, description="Snapshot of arrangement metadata."
    )

    # ── Add-ons & timing ──────────────────────────────────────────────
    addons: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Add-on snapshots: [{"addon_id": "...", "name": "...", "price": "0.000", "duration": 30}]',
    )
    extra_time: int = Field(default=0, ge=0, description="Extra time in minutes.")
    total_duration: int = Field(default=0, ge=0, description="Total service duration in minutes.")

    # ── Financial ─────────────────────────────────────────────────────
    total_amount: Decimal = Field(description="Amount charged for the voucher (KWD).")
    currency: str = Field(default="KWD", max_length=3, description="Currency code.")

    # ── Recipient ─────────────────────────────────────────────────────
    recipient_phone: str | None = Field(
        default=None,
        max_length=50,
        description="Recipient phone number — used for SMS delivery of the secret code.",
    )
    recipient_id: uuid.UUID | None = Field(
        default=None,
        description="Optional UUID of the recipient customer in ushauth.",
    )
    recipient_data: RecipientDetails = Field(
        default_factory=RecipientDetails,
        description="Recipient contact snapshot.",
    )

    # ── Sender (buyer) ────────────────────────────────────────────────
    sender_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID of the customer purchasing/sending the voucher. "
            "If omitted, defaults to the authenticated user's ID from user token."
        ),
    )
    sender_data: SenderDetails = Field(
        default_factory=SenderDetails,
        description="Sender's name and phone number snapshot.",
    )

    # ── Extra-time pricing ────────────────────────────────────────────
    price_for_extra_time: Decimal | None = Field(
        default=None,
        description="Price per extra-time unit (nullable — uses service default if omitted).",
    )

    # ── Personalisation ───────────────────────────────────────────────
    gift_message: str | None = Field(
        default=None, description="Optional personalised gift message."
    )
    gift_from: str | None = Field(
        default=None, description="Optional name or signature of the gift giver."
    )
    gift_template: str | None = Field(
        default=None, max_length=100, description="Optional gift card template identifier."
    )

    # ── Category ──────────────────────────────────────────────────────────
    gift_category: str = Field(
        default=GiftCategory.SERVICE.value,
        description="Category of the gift voucher: 'digital', 'physical', or 'service'. Defaults to 'service'.",
    )

    # ── Delivery & Items (optional) ───────────────────────────────────
    ordered_items: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional ordered items list (products, services, digital items).",
    )
    delivery_status: str | None = Field(
        default=None,
        description="Optional delivery status ('ordered', 'ready_to_go', 'on_the_way', 'delivered', 'received').",
    )
    delivery_address: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured delivery address dictionary.",
    )

    # ── Digital Product Data (optional) ───────────────────────────────
    digital_product_data: dict[str, Any] | None = Field(
        default=None,
        description="Optional digital product metadata snapshot (JSONB).",
    )
    is_digital_gift_opened: bool = Field(
        default=False,
        description="Whether the digital gift has been opened/revealed by recipient (default: False).",
    )

    # ── Lifecycle & Validity ──────────────────────────────────────────
    status: str | None = Field(
        default=None,
        description=(
            "Initial voucher status: 'created', 'payment_pending', or 'active'. "
            "Defaults to 'created' (or 'active' if payment is already confirmed)."
        ),
    )
    expire_date: datetime | None = Field(
        default=None,
        description="Optional explicit expiration date/time (ISO 8601). Defaults to 60 days from now.",
    )
    validity_days: int | None = Field(
        default=60,
        ge=1,
        description="Optional validity period in days from creation. Defaults to 60 days.",
    )
    validity: int | None = Field(
        default=None,
        ge=1,
        description="Alias for validity_days. Defaults to 60 days if neither validity nor expire_date is provided.",
    )

    # ── Authoring ─────────────────────────────────────────────────────
    created_by: uuid.UUID | None = Field(
        default=None,
        description="Optional UUID of the staff or admin user who created this voucher.",
    )

    # ── Optional cross-references ─────────────────────────────────────
    booking_id: uuid.UUID | None = Field(
        default=None,
        description="Optional booking that triggered creation of this voucher.",
    )
    booking_data: dict[str, Any] | None = Field(
        default=None,
        description="Optional booking data snapshot that triggered creation of this voucher.",
    )

    # ── Payment fields ────────────────────────────────────────────────
    payment_id: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Payment gateway transaction/reference ID (e.g. '100624710000000255', invoice ID, KNET ref). "
            "Provided if payment has already been processed."
        ),
    )
    payment_data: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Full payment provider response snapshot (JSONB) stored for complete audit trail. "
            "Contains gateway invoice details, payment method, transaction status, etc."
        ),
    )
    payment_url: str | None = Field(
        default=None,
        description="Optional payment gateway redirect/checkout URL.",
    )
    payment_provider: str | None = Field(
        default=None,
        description=(
            "Payment gateway used to process this voucher. "
            "Valid values: MyFatoorah, DirectLink, Deema, Other."
        ),
    )
    payment_through: str | None = Field(
        default=None,
        description=(
            "Sales channel for this voucher. "
            "Valid values: ushspa (app/web), desk (reception/front desk)."
        ),
    )

    @field_validator(
        "service_id",
        "branch_id",
        "service_arrangement_id",
        "recipient_id",
        "sender_id",
        "created_by",
        "booking_id",
        "booking_data",
        "payment_data",
        "payment_id",
        "payment_url",
        "gift_message",
        "gift_from",
        "gift_template",
        "recipient_phone",
        "expire_date",
        "validity_days",
        "validity",
        mode="before",
    )
    @classmethod
    def coerce_empty_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("currency")
    @classmethod
    def normalise_currency(cls, v: str) -> str:
        return "KWD" if v in ("KD", "KWD") else v.upper()

    @field_validator("gift_category", mode="before")
    @classmethod
    def validate_gift_category(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return GiftCategory.SERVICE.value
        normalised = GiftCategory.normalise(str(v))
        valid = [c.value for c in GiftCategory]
        if normalised not in valid:
            raise ValueError(f"Invalid gift_category {v!r}. Valid values: {valid}")
        return normalised

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = GiftVoucherStatus.normalise(str(v))
        try:
            return GiftVoucherStatus(normalised).value
        except ValueError:
            valid = [s.value for s in GiftVoucherStatus]
            raise ValueError(f"Invalid status {v!r}. Valid values: {valid}")

    @field_validator("payment_provider", mode="before")
    @classmethod
    def validate_payment_provider(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = VoucherPaymentProvider.normalise(str(v))
        valid = [p.value for p in VoucherPaymentProvider]
        if normalised not in valid:
            raise ValueError(f"Invalid payment_provider {v!r}. Valid values: {valid}")
        return normalised

    @field_validator("payment_through", mode="before")
    @classmethod
    def validate_payment_through(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = VoucherPaymentThrough.normalise(str(v))
        valid = [p.value for p in VoucherPaymentThrough]
        if normalised not in valid:
            raise ValueError(f"Invalid payment_through {v!r}. Valid values: {valid}")
        return normalised

    model_config = {
        "json_schema_extra": {
            "example": {
                "service_id": "99999999-9999-9999-9999-999999999999",
                "service_data": {
                    "name": "Swedish Massage 60 min",
                    "category": "Massage",
                },
                "branch_id": "11111111-1111-1111-1111-111111111111",
                "branch_data": {
                    "name": "Salmiya Branch",
                },
                "service_arrangement_id": "22222222-2222-2222-2222-222222222222",
                "service_arrangement_data": {
                    "room": "VIP Suite 1",
                },
                "addons": [
                    {
                        "addon_id": "33333333-3333-3333-3333-333333333333",
                        "name": "Aromatherapy Oil",
                        "price": "5.000",
                        "duration": 15,
                    }
                ],
                "extra_time": 15,
                "price_for_extra_time": "5.000",
                "total_duration": 75,
                "total_amount": "45.000",
                "currency": "KWD",
                "sender_id": "44444444-4444-4444-4444-444444444444",
                "sender_data": {
                    "name": "Ahmad Al-Sabah",
                    "phone_number": "+96599123456",
                },
                "recipient_phone": "+96598765432",
                "recipient_id": "55555555-5555-5555-5555-555555555555",
                "recipient_data": {
                    "name": "Fatima Al-Ali",
                    "email": "fatima@example.com",
                    "phone_number": "+96598765432",
                },
                "gift_message": "Happy Birthday! Enjoy your relaxing spa day.",
                "gift_template": "birthday_gold",
                "status": "created",
                "expire_date": "2026-12-31T23:59:59Z",
                "payment_id": "100624710000000255",
                "payment_data": {
                    "invoiceId": "100624710000000255",
                    "paymentMethod": "KNET",
                    "transactionStatus": "SUCCESS",
                },
                "payment_url": "https://portal.myfatoorah.com/knet/100624710000000255",
                "payment_provider": "MyFatoorah",
                "payment_through": "ushspa",
                "booking_id": None,
                "booking_data": None,
                "created_by": None,
            }
        }
    }


class UpdateGiftVoucherStatusRequest(BaseModel):
    """
    PATCH /api/v1/vouchers/{voucher_id}/status/

    Used internally by the payment webhook and the booking confirmation
    flow to advance the voucher status.
    """

    status: str = Field(
        description=(
            "Target status. Valid values: "
            "payment_pending, active, redeemed, fulfilled, expired, cancelled."
        )
    )
    # payment_id is a gateway reference string (e.g. "100624710000000255"),
    # not a UUID. Set when status becomes 'active'.
    payment_id: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Set when status becomes 'active' — the confirmed payment reference "
            "from the payment gateway (e.g. '100624710000000255')."
        ),
    )
    # Full payment provider response stored alongside the reference ID.
    payment_data: dict[str, Any] | None = Field(
        default=None,
        description="Full payment provider response snapshot for audit. Set alongside payment_id.",
    )
    payment_url: str | None = Field(
        default=None,
        description="Payment gateway redirect/checkout URL.",
    )
    booking_id: uuid.UUID | None = Field(
        default=None,
        description="Set when status becomes 'redeemed' — the booking UUID used for redemption.",
    )
    booking_data: dict[str, Any] | None = Field(
        default=None,
        description="Optional booking data snapshot when redeeming or updating voucher.",
    )
    redeemed_by: uuid.UUID | None = Field(
        default=None,
        description="UUID of the user redeeming the voucher. If omitted when status is 'redeemed', auto-populated from API requester.",
    )
    payment_provider: str | None = Field(
        default=None,
        description=(
            "Payment gateway used to process this voucher. "
            "Valid values: MyFatoorah, DirectLink, Deema, Other."
        ),
    )
    payment_through: str | None = Field(
        default=None,
        description=(
            "Sales channel for this voucher. "
            "Valid values: ushspa (app/web), desk (reception/front desk)."
        ),
    )
    ordered_items: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional ordered items list update.",
    )
    delivery_status: str | None = Field(
        default=None,
        description="Optional delivery status update ('ordered', 'ready_to_go', 'on_the_way', 'delivered', 'received').",
    )
    delivery_address: dict[str, Any] | None = Field(
        default=None,
        description="Optional delivery address update.",
    )

    @field_validator(
        "booking_id",
        "booking_data",
        "redeemed_by",
        "payment_id",
        "payment_url",
        "payment_data",
        mode="before",
    )
    @classmethod
    def coerce_empty_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("Status cannot be empty.")
        normalised = GiftVoucherStatus.normalise(str(v))
        try:
            return GiftVoucherStatus(normalised).value
        except ValueError:
            valid = [s.value for s in GiftVoucherStatus]
            raise ValueError(f"Invalid status {v!r}. Valid values: {valid}")

    @field_validator("payment_provider", mode="before")
    @classmethod
    def validate_payment_provider(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = VoucherPaymentProvider.normalise(str(v))
        valid = [p.value for p in VoucherPaymentProvider]
        if normalised not in valid:
            raise ValueError(f"Invalid payment_provider {v!r}. Valid values: {valid}")
        return normalised

    @field_validator("payment_through", mode="before")
    @classmethod
    def validate_payment_through(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = VoucherPaymentThrough.normalise(str(v))
        valid = [p.value for p in VoucherPaymentThrough]
        if normalised not in valid:
            raise ValueError(f"Invalid payment_through {v!r}. Valid values: {valid}")
        return normalised


class UpdateVoucherDeliveryStatusRequest(BaseModel):
    """
    Payload to advance the voucher delivery status.

    Valid transitions: ordered → ready_to_go → on_the_way → delivered → received.
    Accepts `status` or `delivery_status`.
    """

    status: str = Field(
        default="",
        description="New delivery status: 'ordered', 'ready_to_go', 'on_the_way', 'delivered', 'received'.",
    )
    delivery_status: str | None = Field(
        default=None,
        description="Optional alias for status.",
    )
    secret_code: str | None = Field(
        default=None,
        description="Secret code of the voucher. Required for public delivery status update requests.",
    )
    note: str | None = Field(default=None, max_length=500, description="Optional delivery transition note.")

    @model_validator(mode="before")
    @classmethod
    def resolve_status(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Unwrap nested body if client sent {"body": {...}}
            if "body" in data and isinstance(data["body"], dict):
                inner = data["body"]
                data = {**data, **inner}

            s_val = data.get("status")
            s_str = s_val.strip() if isinstance(s_val, str) else ""

            d_val = data.get("delivery_status")
            d_str = d_val.strip() if isinstance(d_val, str) else ""

            target_status = s_str or d_str
            if target_status:
                normalised = target_status.lower()
                data["status"] = normalised
                data["delivery_status"] = normalised
            elif "status" in data and not s_str and not d_str:
                data["status"] = ""

            if "secret_code" in data and isinstance(data["secret_code"], str):
                data["secret_code"] = data["secret_code"].strip()
        return data

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        val = (v or "").strip().lower()
        valid = [s.value for s in DeliveryStatus]
        if val not in valid:
            raise ValueError(f"Invalid delivery_status {v!r}. Valid values: {valid}")
        return val


class VerifyVoucherSecretCodeRequest(BaseModel):
    """
    POST /vouchers/public/{public_token}/
    Payload containing secret_code to verify against public_token.
    """

    secret_code: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="The secret code for the gift voucher.",
    )



class UpdateGiftVoucherRequest(BaseModel):
    """
    PATCH /api/v1/vouchers/{voucher_id}/ or PUT /api/v1/vouchers/{voucher_id}/

    Update an existing gift voucher. Supports partial or full updates.
    """

    gift_category: str | None = Field(
        default=None,
        description="Category of the gift voucher: 'digital', 'physical', or 'service'.",
    )
    ordered_items: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional ordered items list (products, services, digital items).",
    )
    delivery_status: str | None = Field(
        default=None,
        description="Optional delivery status ('ordered', 'ready_to_go', 'on_the_way', 'delivered', 'received').",
    )
    delivery_address: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured delivery address dictionary.",
    )
    digital_product_data: dict[str, Any] | None = Field(
        default=None,
        description="Optional digital product metadata snapshot update (JSONB).",
    )
    is_digital_gift_opened: bool | None = Field(
        default=None,
        description="Optional update to whether the digital gift has been opened.",
    )
    service_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the service being gifted.",
    )
    service_data: dict[str, Any] | None = Field(
        default=None,
        description="Snapshot of service metadata (name, category, etc.).",
    )
    branch_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the branch where the service will be rendered.",
    )
    branch_data: dict[str, Any] | None = Field(
        default=None,
        description="Snapshot of branch metadata.",
    )
    service_arrangement_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the service arrangement (room/package).",
    )
    service_arrangement_data: dict[str, Any] | None = Field(
        default=None,
        description="Snapshot of arrangement metadata.",
    )
    addons: list[dict[str, Any]] | None = Field(
        default=None,
        description="Add-on snapshots list.",
    )
    extra_time: int | None = Field(
        default=None,
        ge=0,
        description="Extra time in minutes.",
    )
    price_for_extra_time: Decimal | None = Field(
        default=None,
        description="Price per extra-time unit.",
    )
    total_duration: int | None = Field(
        default=None,
        ge=0,
        description="Total service duration in minutes.",
    )
    total_amount: Decimal | None = Field(
        default=None,
        description="Amount charged for the voucher (KWD).",
    )
    currency: str | None = Field(
        default=None,
        max_length=3,
        description="Currency code.",
    )
    sender_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the customer purchasing/sending the voucher.",
    )
    sender_data: dict[str, Any] | None = Field(
        default=None,
        description="Sender's name and phone number snapshot.",
    )
    recipient_phone: str | None = Field(
        default=None,
        max_length=50,
        description="Recipient phone number.",
    )
    recipient_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the recipient customer in ushauth.",
    )
    recipient_data: dict[str, Any] | None = Field(
        default=None,
        description="Recipient contact snapshot.",
    )
    gift_message: str | None = Field(
        default=None,
        description="Personalised gift message.",
    )
    gift_from: str | None = Field(
        default=None,
        description="Name or signature of the gift giver.",
    )
    gift_template: str | None = Field(
        default=None,
        max_length=100,
        description="Visual card template theme identifier.",
    )
    status: str | None = Field(
        default=None,
        description="Target voucher status: 'created', 'payment_pending', 'active', 'redeemed', 'fulfilled', 'expired', 'cancelled'.",
    )
    expire_date: datetime | None = Field(
        default=None,
        description="Explicit expiration date/time (ISO 8601).",
    )
    booking_id: uuid.UUID | None = Field(
        default=None,
        description="Booking reference UUID.",
    )
    booking_data: dict[str, Any] | None = Field(
        default=None,
        description="Booking data snapshot.",
    )
    payment_id: str | None = Field(
        default=None,
        max_length=100,
        description="Payment gateway transaction/reference ID.",
    )
    payment_data: dict[str, Any] | None = Field(
        default=None,
        description="Full payment provider response snapshot (JSONB).",
    )
    payment_url: str | None = Field(
        default=None,
        description="Payment gateway checkout/redirect URL.",
    )
    payment_provider: str | None = Field(
        default=None,
        description="Payment gateway used (MyFatoorah, DirectLink, Deema, Other).",
    )
    payment_through: str | None = Field(
        default=None,
        description="Sales channel (ushspa, desk).",
    )
    redeemed_by: uuid.UUID | None = Field(
        default=None,
        description="UUID of the user redeeming the voucher.",
    )

    @field_validator(
        "branch_id",
        "service_arrangement_id",
        "recipient_id",
        "sender_id",
        "booking_id",
        "booking_data",
        "payment_data",
        "payment_id",
        "payment_url",
        "gift_message",
        "gift_from",
        "gift_template",
        "recipient_phone",
        "expire_date",
        "redeemed_by",
        "digital_product_data",
        mode="before",
    )
    @classmethod
    def coerce_empty_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("currency")
    @classmethod
    def normalise_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return "KWD" if v in ("KD", "KWD") else v.upper()

    @field_validator("gift_category", mode="before")
    @classmethod
    def validate_gift_category(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = GiftCategory.normalise(str(v))
        valid = [c.value for c in GiftCategory]
        if normalised not in valid:
            raise ValueError(f"Invalid gift_category {v!r}. Valid values: {valid}")
        return normalised

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = GiftVoucherStatus.normalise(str(v))
        try:
            return GiftVoucherStatus(normalised).value
        except ValueError:
            valid = [s.value for s in GiftVoucherStatus]
            raise ValueError(f"Invalid status {v!r}. Valid values: {valid}")

    @field_validator("delivery_status", mode="before")
    @classmethod
    def validate_delivery_status(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = str(v).strip().lower()
        valid = [s.value for s in DeliveryStatus]
        if normalised not in valid:
            raise ValueError(f"Invalid delivery_status {v!r}. Valid values: {valid}")
        return normalised

    @field_validator("payment_provider", mode="before")
    @classmethod
    def validate_payment_provider(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = VoucherPaymentProvider.normalise(str(v))
        valid = [p.value for p in VoucherPaymentProvider]
        if normalised not in valid:
            raise ValueError(f"Invalid payment_provider {v!r}. Valid values: {valid}")
        return normalised

    @field_validator("payment_through", mode="before")
    @classmethod
    def validate_payment_through(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        normalised = VoucherPaymentThrough.normalise(str(v))
        valid = [p.value for p in VoucherPaymentThrough]
        if normalised not in valid:
            raise ValueError(f"Invalid payment_through {v!r}. Valid values: {valid}")
        return normalised

    model_config = {
        "extra": "ignore",
        "json_schema_extra": {
            "example": {
                "gift_message": "Enjoy your special spa day!",
                "total_amount": "50.000",
                "recipient_phone": "+96598765432",
                "status": "active",
            }
        },
    }


# ── Response schemas ──────────────────────────────────────────────────────────


class GiftVoucherResponse(BaseModel):
    """Full detail response — returned to the sender and admin."""

    id: uuid.UUID
    voucher_number: str | None = None
    gift_category: str = GiftCategory.SERVICE.value
    ordered_items: list[dict[str, Any]] | None = None
    delivery_status: str | None = None
    delivery_status_label: str | None = None
    delivery_status_label_ar: str | None = None
    delivery_address: dict[str, Any] | None = None
    digital_product_data: dict[str, Any] | None = None
    is_digital_gift_opened: bool = False
    service_id: uuid.UUID | None = None
    service_data: dict[str, Any]
    branch_id: uuid.UUID | None
    branch_data: dict[str, Any]
    service_arrangement_id: uuid.UUID | None
    service_arrangement_data: dict[str, Any]
    addons: list[dict[str, Any]]
    extra_time: int
    price_for_extra_time: Decimal | None = None
    expire_date: datetime
    status: str
    sender_id: uuid.UUID
    sender_data: dict[str, Any]
    recipient_phone: str | None
    recipient_id: uuid.UUID | None = None
    recipient_data: dict[str, Any]
    created_by: uuid.UUID | None
    total_duration: int
    total_amount: str  # String to preserve KWD 3-decimal precision
    currency: str
    gift_message: str | None = None
    gift_from: str | None = None
    gift_template: str | None = None
    # secret_code is intentionally included in the full response for the sender
    # so they can see the code that was sent to the recipient.
    secret_code: str
    public_token: str
    redeemed_booking_id: uuid.UUID | None
    redeemed_at: datetime | None
    redeemed_by: uuid.UUID | None = None
    booking_id: uuid.UUID | None
    booking_data: dict[str, Any] | None = None
    # payment_id is a plain gateway reference string (not UUID)
    payment_id: str | None
    payment_data: dict[str, Any] | None
    payment_url: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GiftVoucherPublicResponse(BaseModel):
    """
    Public page response — no authentication required, no secret_code.

    Used by the gift card recipient public page URL:
    https://example.com/gifts/{public_token}
    """

    id: uuid.UUID
    voucher_number: str | None = None
    gift_category: str = GiftCategory.SERVICE.value
    ordered_items: list[dict[str, Any]] | None = None
    delivery_status: str | None = None
    delivery_status_label: str | None = None
    delivery_status_label_ar: str | None = None
    delivery_address: dict[str, Any] | None = None
    digital_product_data: dict[str, Any] | None = None
    is_digital_gift_opened: bool = False
    service_id: uuid.UUID | None = None
    service_data: dict[str, Any]
    branch_id: uuid.UUID | None
    branch_data: dict[str, Any]
    service_arrangement_id: uuid.UUID | None
    service_arrangement_data: dict[str, Any]
    addons: list[dict[str, Any]]
    extra_time: int
    expire_date: datetime
    status: str
    # Sender: only show name, not full contact details
    sender_data: dict[str, Any]
    total_duration: int
    total_amount: str
    currency: str
    gift_message: str | None = None
    gift_from: str | None = None
    gift_template: str | None = None
    public_token: str
    # secret_code is OMITTED from the public response
    # payment_id / payment_data are OMITTED from the public response
    created_at: datetime

    model_config = {"from_attributes": True}


class GiftVoucherListItem(BaseModel):
    """Lightweight list item for paginated responses."""

    id: uuid.UUID
    voucher_number: str | None = None
    gift_category: str = GiftCategory.SERVICE.value
    ordered_items: list[dict[str, Any]] | None = None
    delivery_status: str | None = None
    delivery_status_label: str | None = None
    delivery_status_label_ar: str | None = None
    delivery_address: dict[str, Any] | None = None
    digital_product_data: dict[str, Any] | None = None
    is_digital_gift_opened: bool = False
    service_id: uuid.UUID | None = None
    service_data: dict[str, Any]
    branch_id: uuid.UUID | None
    branch_data: dict[str, Any] = Field(default_factory=dict)
    status: str
    total_amount: str
    currency: str
    expire_date: datetime
    extra_time: int = 0
    price_for_extra_time: Decimal | None = None
    sender_data: dict[str, Any] = Field(default_factory=dict)
    recipient_phone: str | None
    recipient_id: uuid.UUID | None = None
    recipient_data: dict[str, Any]
    gift_message: str | None = None
    gift_from: str | None = None
    secret_code: str | None = None
    public_token: str
    redeemed_at: datetime | None
    redeemed_by: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    booking_data: dict[str, Any] | None = None
    # payment_id is a plain gateway reference string (not UUID)
    payment_id: str | None
    payment_data: dict[str, Any] | None
    payment_url: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

