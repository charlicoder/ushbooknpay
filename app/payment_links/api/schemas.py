"""app/payment_links/api/schemas.py — request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.payment_links.infrastructure.models import PaymentLink


class CreatePaymentLinkRequest(BaseModel):
    # ``voucher`` is accepted as an alias of ``gift_voucher``.
    target_type: Literal["booking", "voucher", "gift_voucher", "product"]
    target_id: uuid.UUID
    amount: Decimal | None = Field(
        default=None, gt=0, description="Amount for the payment link; defaults to target's total_amount or refund amount."
    )
    invoice_id: str | None = Field(
        default=None, max_length=64, description="Accounting invoice id (resolved server-side when omitted)."
    )
    invoice_number: str | None = Field(
        default=None, max_length=64, description="Accounting invoice number, e.g. INV/2026/10/00001."
    )
    force_new: bool = Field(default=False, description="Cancel any open link and issue a fresh one.")
    expiry_minutes: int | None = Field(default=None, ge=5, le=10080)
    gateway: str | None = Field(default=None, description="Gateway key; defaults to the configured gateway.")

    @field_validator("invoice_id", "invoice_number", mode="before")
    @classmethod
    def _strip_empty_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v


class RefundPaymentRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, description="Omit for a full refund of the remaining balance.")
    reason: str | None = Field(default=None, max_length=500)


class PaymentLinkOut(BaseModel):
    id: uuid.UUID
    link_number: str | None
    status: str
    payment_url: str | None
    gateway: str
    provider: str
    gateway_invoice_id: str | None
    invoice_id: str | None
    invoice_number: str | None
    amount: Decimal
    amount_refunded: Decimal
    currency: str
    target_type: str
    target_id: uuid.UUID
    target_ref: str | None
    expires_at: datetime | None
    is_expired: bool
    paid_at: datetime | None
    created_at: datetime | None
    needs_attention: str | None = None

    @classmethod
    def from_link(cls, link: PaymentLink) -> "PaymentLinkOut":
        open_link = link.status in ("created", "pending", "failed")
        return cls(
            id=link.id,
            link_number=link.link_number,
            status=link.status,
            payment_url=link.payment_url,
            gateway=link.gateway,
            provider=link.provider_label,
            gateway_invoice_id=link.gateway_invoice_id,
            invoice_id=link.invoice_id,
            invoice_number=link.invoice_number,
            amount=link.amount,
            amount_refunded=link.amount_refunded or Decimal("0"),
            currency=link.currency,
            target_type=link.target_type,
            target_id=link.target_id,
            target_ref=link.target_ref,
            expires_at=link.expires_at,
            is_expired=bool(open_link and link.expires_at and link.expires_at <= datetime.now(timezone.utc)),
            paid_at=link.paid_at,
            created_at=link.created_at,
            needs_attention=(link.meta or {}).get("needs_attention"),
        )


class RefundOut(BaseModel):
    refund_id: uuid.UUID
    refund_number: str
    status: str
    refunded_amount: Decimal
    currency: str
    gateway_refund_id: str | None
