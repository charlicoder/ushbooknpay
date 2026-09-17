from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.gifts.domain.value_objects import (
    GiftCartStatus,
    GiftDeliveryStatus,
    GiftPurchaseStatus,
    GiftType,
)

def _default_expire_date() -> datetime:
    try:
        from app.core.config import get_settings
        days = get_settings().GIFT_VOUCHER_EXPIRE_DAYS
    except Exception:
        days = 60
    return datetime.now(tz=timezone.utc) + timedelta(days=days)

def _generate_public_token() -> str:
    return secrets.token_urlsafe(32)

class GiftVoucherCart(Base):
    __tablename__ = "gift_voucher_carts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    gift_type: Mapped[str] = mapped_column(String(20), nullable=False, default=GiftType.DIGITAL.value, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=GiftCartStatus.ACTIVE.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    items: Mapped[list[GiftVoucherCartItem]] = relationship("GiftVoucherCartItem", back_populates="cart", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_gift_carts_customer_status", "customer_id", "status"),)

class GiftVoucherCartItem(Base):
    __tablename__ = "gift_voucher_cart_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cart_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_carts.id", ondelete="CASCADE"), nullable=False)
    gift_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    digital_gift_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    digital_gift_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    service_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_arrangement_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    addons: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    extra_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_for_extra_minutes: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=3), nullable=True)
    selected_video_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    custom_video_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    product_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=3), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    cart: Mapped[GiftVoucherCart] = relationship("GiftVoucherCart", back_populates="items")
    __table_args__ = (Index("ix_gift_cart_items_cart", "cart_id"),)

class GiftVoucherPurchase(Base):
    __tablename__ = "gift_voucher_purchases"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gift_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    digital_gift_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    digital_gift_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=GiftPurchaseStatus.PENDING_PAYMENT.value, index=True)
    expire_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_default_expire_date)
    sender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    sender_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    recipient_phone: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    recipient_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    recipient_language: Mapped[str] = mapped_column(String(10), nullable=False, default="ar")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=3), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")
    gift_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    gift_template: Mapped[str | None] = mapped_column(String(100), nullable=True)
    secret_code_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    public_token: Mapped[str] = mapped_column(String(64), nullable=False, default=_generate_public_token, unique=True)
    payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    payment_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_through: Mapped[str | None] = mapped_column(String(10), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    items: Mapped[list[GiftVoucherPurchaseItem]] = relationship("GiftVoucherPurchaseItem", back_populates="purchase", cascade="all, delete-orphan")
    recipient_record: Mapped[GiftVoucherRecipient | None] = relationship("GiftVoucherRecipient", back_populates="purchase", uselist=False)
    delivery: Mapped[GiftVoucherDelivery | None] = relationship("GiftVoucherDelivery", back_populates="purchase", uselist=False)
    verification: Mapped[GiftVoucherVerification | None] = relationship("GiftVoucherVerification", back_populates="purchase", uselist=False)
    redemptions: Mapped[list[GiftVoucherRedemption]] = relationship("GiftVoucherRedemption", back_populates="purchase")
    status_history: Mapped[list[GiftVoucherStatusHistory]] = relationship("GiftVoucherStatusHistory", back_populates="purchase", order_by="GiftVoucherStatusHistory.created_at")
    __table_args__ = (
        UniqueConstraint("public_token", name="uq_gift_purchases_public_token"),
        Index("ix_gift_purchases_payment_id", "payment_id"),
        Index("ix_gift_purchases_sender_status", "sender_id", "status"),
        Index("ix_gift_purchases_recipient_phone", "recipient_phone"),
        Index("ix_gift_purchases_gift_type", "gift_type"),
        Index("ix_gift_purchases_expire_date", "expire_date"),
        Index("ix_gift_purchases_status", "status"),
    )

class GiftVoucherPurchaseItem(Base):
    __tablename__ = "gift_voucher_purchase_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    gift_type: Mapped[str] = mapped_column(String(20), nullable=False)
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_arrangement_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    addons: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    extra_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_for_extra_minutes: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=3), nullable=True)
    total_duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_video_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    custom_video_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    product_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=3), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(precision=10, scale=3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    purchase: Mapped[GiftVoucherPurchase] = relationship("GiftVoucherPurchase", back_populates="items")

class GiftVoucherRecipient(Base):
    __tablename__ = "gift_voucher_recipients"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_purchases.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="ar")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    purchase: Mapped[GiftVoucherPurchase] = relationship("GiftVoucherPurchase", back_populates="recipient_record")

class GiftVoucherDelivery(Base):
    __tablename__ = "gift_voucher_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_purchases.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=GiftDeliveryStatus.PROCESSING.value, index=True)
    tracking_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    purchase: Mapped[GiftVoucherPurchase] = relationship("GiftVoucherPurchase", back_populates="delivery")

class GiftVoucherVerification(Base):
    __tablename__ = "gift_voucher_verifications"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_purchases.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    purchase: Mapped[GiftVoucherPurchase] = relationship("GiftVoucherPurchase", back_populates="verification")

class GiftVoucherRedemption(Base):
    __tablename__ = "gift_voucher_redemptions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_purchases.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    booking_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    purchase: Mapped[GiftVoucherPurchase] = relationship("GiftVoucherPurchase", back_populates="redemptions")
    __table_args__ = (UniqueConstraint("purchase_id", name="uq_gift_redemptions_purchase"),)

class GiftVoucherStatusHistory(Base):
    __tablename__ = "gift_voucher_status_history"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("gift_voucher_purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    purchase: Mapped[GiftVoucherPurchase] = relationship("GiftVoucherPurchase", back_populates="status_history")
