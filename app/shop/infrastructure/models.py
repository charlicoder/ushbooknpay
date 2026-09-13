"""
app/shop/infrastructure/models.py
───────────────────────────────────
SQLAlchemy ORM models for the Shop domain.

Tables:
    shop_orders              — one row per customer order
    shop_order_items         — line items (product snapshot at order time)
    shop_order_status_history — append-only audit log of delivery status changes
"""

from __future__ import annotations

import uuid
from typing import Any
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shop.domain.value_objects import DeliveryStatus, OrderPaymentStatus


class ShopOrder(Base):
    """
    A customer's product order.

    Prices are snapshots from ushauth at order creation time.
    This record is the source of truth for billing and delivery.
    """

    __tablename__ = "shop_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Human-readable order number (ORD-YYYY-NNNN)
    order_number: Mapped[str] = mapped_column(
        String(30), nullable=False, unique=True, index=True
    )

    # ── Customer identity (snapshot from JWT at creation time) ────────────
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    customer_phone: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    # Contact number explicitly supplied in the order (may differ from JWT phone)
    contact_number: Mapped[str] = mapped_column(String(30), nullable=False, default="")

    # ── Structured delivery address ───────────────────────────────────────
    area: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    block: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    street: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    building_no: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    floor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    apartment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    delivery_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DeliveryStatus.ORDERED.value, index=True
    )
    # 8-character alphanumeric secret sent to customer for self-service "received" update
    tracking_code: Mapped[str] = mapped_column(
        String(12), nullable=False, unique=True, index=True
    )

    @property
    def formatted_address(self) -> str:
        """Human-readable single-line address for notifications and SQS events."""
        parts = [
            f"Area: {self.area}" if self.area else "",
            f"Block: {self.block}" if self.block else "",
            f"Street: {self.street}" if self.street else "",
            f"Bldg: {self.building_no}" if self.building_no else "",
            f"Floor: {self.floor}" if self.floor else "",
            f"Apt: {self.apartment}" if self.apartment else "",
            f"City: {self.city}" if self.city else "",
        ]
        return ", ".join(p for p in parts if p)

    # ── Pricing (KWD, all as Decimal) ─────────────────────────────────────
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    discount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")

    # ── Payment (updated externally by frontend/ushdesk) ──────────────────
    payment_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=OrderPaymentStatus.NOT_INITIATED.value,
        index=True,
    )

    # ── Internal notes ────────────────────────────────────────────────────
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────
    items: Mapped[list["ShopOrderItem"]] = relationship(
        "ShopOrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    status_history: Mapped[list["ShopOrderStatusHistory"]] = relationship(
        "ShopOrderStatusHistory",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="ShopOrderStatusHistory.created_at",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_shop_orders_customer_status", "customer_id", "delivery_status"),
        Index("ix_shop_orders_created_at", "created_at"),
    )


class ShopOrderItem(Base):
    """
    A single line item in a shop order.

    Product fields are snapshotted at order creation time so historical
    orders remain accurate even if product data changes in ushauth.
    """

    __tablename__ = "shop_order_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shop_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Logical reference to ushauth product — no FK (cross-service)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Snapshots for historical accuracy and multilingual display
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_name_ar: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # Primary product image URL — snapshotted at order time from ushauth product.image1
    product_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    line_total: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")

    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    order: Mapped["ShopOrder"] = relationship("ShopOrder", back_populates="items")

    __table_args__ = (
        Index("ix_shop_order_items_product", "product_id"),
    )


class ShopOrderStatusHistory(Base):
    """
    Append-only log of every delivery status change for an order.

    Never updated — only created.
    """

    __tablename__ = "shop_order_status_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shop_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    # "customer", "staff", "system"
    changed_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    order: Mapped["ShopOrder"] = relationship(
        "ShopOrder", back_populates="status_history"
    )
