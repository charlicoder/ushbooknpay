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

from pydantic import BaseModel, Field, field_validator

from app.voucher.domain.value_objects import (
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
    service_id: uuid.UUID = Field(description="UUID of the service being gifted.")
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

    # ── Sender details snapshot ───────────────────────────────────────
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
    gift_template: str | None = Field(
        default=None, max_length=100, description="Optional gift card template identifier."
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

    @field_validator("currency")
    @classmethod
    def normalise_currency(cls, v: str) -> str:
        return "KWD" if v in ("KD", "KWD") else v.upper()

    @field_validator("payment_provider")
    @classmethod
    def validate_payment_provider(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = [p.value for p in VoucherPaymentProvider]
        if v not in valid:
            raise ValueError(f"Invalid payment_provider {v!r}. Valid values: {valid}")
        return v

    @field_validator("payment_through")
    @classmethod
    def validate_payment_through(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = [p.value for p in VoucherPaymentThrough]
        if v not in valid:
            raise ValueError(f"Invalid payment_through {v!r}. Valid values: {valid}")
        return v


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

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        try:
            GiftVoucherStatus(v)
        except ValueError:
            valid = [s.value for s in GiftVoucherStatus]
            raise ValueError(f"Invalid status {v!r}. Valid values: {valid}")
        return v

    @field_validator("payment_provider")
    @classmethod
    def validate_payment_provider(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = [p.value for p in VoucherPaymentProvider]
        if v not in valid:
            raise ValueError(f"Invalid payment_provider {v!r}. Valid values: {valid}")
        return v

    @field_validator("payment_through")
    @classmethod
    def validate_payment_through(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = [p.value for p in VoucherPaymentThrough]
        if v not in valid:
            raise ValueError(f"Invalid payment_through {v!r}. Valid values: {valid}")
        return v


# ── Response schemas ──────────────────────────────────────────────────────────


class GiftVoucherResponse(BaseModel):
    """Full detail response — returned to the sender and admin."""

    id: uuid.UUID
    service_id: uuid.UUID
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
    gift_message: str | None
    gift_template: str | None
    # secret_code is intentionally included in the full response for the sender
    # so they can see the code that was sent to the recipient.
    secret_code: str
    public_token: str
    redeemed_booking_id: uuid.UUID | None
    redeemed_at: datetime | None
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
    service_id: uuid.UUID
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
    gift_message: str | None
    gift_template: str | None
    public_token: str
    # secret_code is OMITTED from the public response
    # payment_id / payment_data are OMITTED from the public response
    created_at: datetime

    model_config = {"from_attributes": True}


class GiftVoucherListItem(BaseModel):
    """Lightweight list item for paginated responses."""

    id: uuid.UUID
    service_id: uuid.UUID
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
    secret_code: str | None = None
    public_token: str
    redeemed_at: datetime | None
    booking_id: uuid.UUID | None = None
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
