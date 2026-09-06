"""
app/voucher/infrastructure/models.py
──────────────────────────────────────
SQLAlchemy ORM model for the Gift Voucher domain.

Design notes:
  - service_id, branch_id, sender_id are plain UUIDs with no FK constraint
    because those entities live in ushauth, a separate service.
  - redeemed_booking_id is an FK → bookings.id (SET NULL on delete) because
    bookings are local.
  - booking_id is the booking that triggered voucher creation (optional, SET NULL).
  - payment_id is stored as a plain String(100) (no FK, no UUID constraint) to
    accommodate gateway-specific reference strings like "100624710000000255"
    (MyFatoorah, Tap, KNET, etc.).
  - payment_data is a JSONB snapshot of the full payment provider response,
    stored alongside payment_id for complete payment audit records.
  - secret_code is a 6-digit random string generated at creation.
  - public_token is a URL-safe token for the non-guessable shareable public page.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.voucher.domain.value_objects import GiftVoucherStatus


def _default_expire_date() -> datetime:
    """Return default expiry: now + GIFT_VOUCHER_EXPIRE_DAYS (default 60)."""
    try:
        from app.core.config import get_settings
        days = get_settings().GIFT_VOUCHER_EXPIRE_DAYS
    except Exception:
        days = 60
    return datetime.now(tz=timezone.utc) + timedelta(days=days)


def _generate_secret_code() -> str:
    """Generate a cryptographically random 6-digit numeric code."""
    # secrets.randbelow ensures uniform distribution without modulo bias
    return f"{secrets.randbelow(1_000_000):06d}"


def _generate_public_token() -> str:
    """Generate a URL-safe non-guessable public page token (~43 chars)."""
    return secrets.token_urlsafe(32)


class GiftVoucher(Base):
    """
    Gift voucher record.

    Lifecycle:
        created → payment_pending → active → redeemed → fulfilled
                                           ↘ expired
                                           ↘ cancelled
        (cancelled and expired are terminal states)

    The secret_code is a 6-digit string sent to the recipient via
    ushnotice (SMS/Email/WhatsApp) after a voucher.active SQS event.

    The public_token powers the shareable public voucher page URL and
    must NEVER be guessable.
    """

    __tablename__ = "gift_vouchers"

    # ── Identity ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Service & Branch snapshot (external refs — no FK) ─────────────────
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    service_arrangement_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    # ── Add-ons & timing ──────────────────────────────────────────────────
    # Format: [{"addon_id": "uuid", "name": "...", "price": "0.000", "duration": 30}]
    addons: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    extra_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Price charged per extra-time unit (decimal, nullable — uses service default if NULL)
    price_for_extra_time: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────
    expire_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_default_expire_date
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=GiftVoucherStatus.CREATED.value,
    )

    # ── Sender (buyer) — plain UUID, no FK ───────────────────────────────
    sender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # {"name": "...", "phone_number": "..."}
    sender_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    # ── Recipient ─────────────────────────────────────────────────────────
    recipient_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Optional: UUID of the recipient customer in ushauth (no FK — cross-service)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # {"name": "...", "email": "...", "phone_number": "..."}
    recipient_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    # ── Authoring ─────────────────────────────────────────────────────────
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # ── Financial ─────────────────────────────────────────────────────────
    total_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[float] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")

    # ── Personalisation ───────────────────────────────────────────────────
    gift_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    gift_template: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── Security tokens ───────────────────────────────────────────────────
    # 6-digit numeric string sent to recipient via SMS/Email (via ushnotice)
    secret_code: Mapped[str] = mapped_column(
        String(6), nullable=False, default=_generate_secret_code
    )
    # Non-guessable URL token for the public gift card page (~43-char URL-safe)
    public_token: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_generate_public_token
    )

    # ── Redemption ────────────────────────────────────────────────────────
    # The booking where this voucher was actually used
    redeemed_booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Cross-references ──────────────────────────────────────────────────
    # Optional: the booking that *triggered* creation of this voucher
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Payment ───────────────────────────────────────────────────────────
    # Stored as a plain string (no UUID, no FK) to accommodate gateway-specific
    # reference strings such as "100624710000000255" (MyFatoorah, Tap, KNET…).
    # Set when payment is confirmed (status → active).
    payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Full payment provider response snapshot stored for complete audit trail.
    payment_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
    # Payment gateway checkout/redirect URL (e.g., invoice URL or hosted payment session)
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Audit timestamps ──────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("public_token", name="uq_gift_vouchers_public_token"),
        Index("ix_gift_vouchers_status", "status"),
        Index("ix_gift_vouchers_sender_id", "sender_id"),
        Index("ix_gift_vouchers_recipient_phone", "recipient_phone"),
        Index("ix_gift_vouchers_expire_date", "expire_date"),
        Index("ix_gift_vouchers_payment_id", "payment_id"),
        Index("ix_gift_vouchers_service_id", "service_id"),
        Index("ix_gift_vouchers_created_at", "created_at"),
        # Composite for "my sent vouchers filtered by status"
        Index("ix_gift_vouchers_sender_status", "sender_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<GiftVoucher id={self.id} status={self.status!r} "
            f"sender={self.sender_id} amount={self.total_amount}>"
        )

    def to_snapshot(self) -> dict:
        """
        Return a serialisable dict snapshot for SQS event payloads.

        Note: secret_code is intentionally included so ushnotice can
        deliver it to the recipient.
        """
        return {
            "id": str(self.id),
            "service_id": str(self.service_id),
            "service_data": self.service_data or {},
            "branch_id": str(self.branch_id) if self.branch_id else None,
            "branch_data": self.branch_data or {},
            "service_arrangement_id": str(self.service_arrangement_id) if self.service_arrangement_id else None,
            "service_arrangement_data": self.service_arrangement_data or {},
            "addons": self.addons or [],
            "extra_time": self.extra_time,
            "price_for_extra_time": str(self.price_for_extra_time) if self.price_for_extra_time is not None else None,
            "expire_date": self.expire_date.isoformat() if self.expire_date else None,
            "status": self.status,
            "sender_id": str(self.sender_id),
            "sender_data": self.sender_data or {},
            "recipient_phone": self.recipient_phone,
            "recipient_id": str(self.recipient_id) if self.recipient_id else None,
            "recipient_data": self.recipient_data or {},
            "created_by": str(self.created_by) if self.created_by else None,
            "total_duration": self.total_duration,
            "total_amount": str(self.total_amount),
            "currency": self.currency,
            "gift_message": self.gift_message,
            "gift_template": self.gift_template,
            "secret_code": self.secret_code,
            "public_token": self.public_token,
            "redeemed_booking_id": str(self.redeemed_booking_id) if self.redeemed_booking_id else None,
            "redeemed_at": self.redeemed_at.isoformat() if self.redeemed_at else None,
            "booking_id": str(self.booking_id) if self.booking_id else None,
            # payment_id is a plain gateway reference string (not UUID)
            "payment_id": self.payment_id or None,
            "payment_data": self.payment_data or {},
            "payment_url": self.payment_url or None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
