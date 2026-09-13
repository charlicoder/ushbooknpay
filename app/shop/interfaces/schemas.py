"""
app/shop/interfaces/schemas.py
────────────────────────────────
Pydantic request/response schemas for the Shop API.

Follows SOLID's Interface Segregation:
  - Separate schemas per use case (staff list, public track, customer create)
  - Multilingual status labels included in every status-bearing response
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.shop.domain.value_objects import (
    DeliveryStatus,
    OrderPaymentStatus,
    get_status_label,
)


# ── Shared primitives ─────────────────────────────────────────────────────────


class OrderItemIn(BaseModel):
    """One product line in a create-order request."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    product_id: uuid.UUID
    # Accept quantity as int OR string (mobile app sends "1")
    quantity: int = Field(ge=1, le=999)

    @field_validator("quantity", mode="before")
    @classmethod
    def coerce_quantity(cls, v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(f"quantity must be a positive integer, got: {v!r}")


class OrderItemOut(BaseModel):
    """Serialised order line item for API responses."""

    id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    product_name_ar: str
    product_image_url: str | None = None
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    currency: str


class StatusHistoryOut(BaseModel):
    """One entry in the delivery status audit log."""

    from_status: str
    from_status_label: str
    from_status_label_ar: str
    to_status: str
    to_status_label: str
    to_status_label_ar: str
    changed_by: str
    note: str | None
    created_at: datetime


# ── Create order ─────────────────────────────────────────────────────────────


class CreateShopOrderRequest(BaseModel):
    """
    Payload to place a new shop order.

    Structured address fields match the Kuwaiti address format used by the
    mobile app and ushdesk.  All extra / unknown fields are silently ignored
    so the frontend never needs schema negotiation.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # ── Required address fields ───────────────────────────────────────────
    area: str = Field(default="", max_length=100, description="Area / neighbourhood")
    block: str = Field(default="", max_length=50, description="Block number")
    street: str = Field(default="", max_length=100, description="Street name or number")
    building_no: str = Field(default="", max_length=50, description="Building or house number")

    # ── Optional address fields ───────────────────────────────────────────
    floor: str | None = Field(default=None, max_length=50)
    apartment: str | None = Field(default=None, max_length=50)
    city: str | None = Field(default=None, max_length=100)
    delivery_notes: str | None = Field(default=None, max_length=500)

    # ── Contact ───────────────────────────────────────────────────────────
    # Stored in the order and used for delivery communications
    contact_number: str | None = Field(default=None, max_length=30)

    # ── Order lines ───────────────────────────────────────────────────────
    items: list[OrderItemIn] = Field(min_length=1)
    internal_notes: str | None = Field(default=None, max_length=500)

    # ── Frontend-computed totals (accepted & ignored) ─────────────────────
    # Backend always recomputes from live product prices
    total_amount: str | None = Field(default=None)
    delivery_charge: str | None = Field(default=None)
    final_amount: str | None = Field(default=None)

    @field_validator("items")
    @classmethod
    def no_duplicate_products(cls, items: list[OrderItemIn]) -> list[OrderItemIn]:
        seen: set[uuid.UUID] = set()
        for item in items:
            if item.product_id in seen:
                raise ValueError(f"Duplicate product_id {item.product_id} in items list.")
            seen.add(item.product_id)
        return items


# ── Update delivery status (staff/agent) ─────────────────────────────────────


class UpdateDeliveryStatusRequest(BaseModel):
    """
    Payload to advance the delivery status.

    Used by delivery agents/staff only (JWT-authenticated).
    Cannot transition to 'received' — that requires the customer's tracking_code.
    """

    status: DeliveryStatus
    note: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def not_received(cls, v: DeliveryStatus) -> DeliveryStatus:
        if v == DeliveryStatus.RECEIVED:
            raise ValueError(
                "Use the /track/{order_number}/received/ endpoint for customer-confirmed receipt."
            )
        return v


# ── Update payment status (staff/ushdesk) ────────────────────────────────────


class UpdatePaymentStatusRequest(BaseModel):
    """
    Update payment status from ushdesk (mark as paid) or mobile (payment success/fail).
    """

    payment_status: OrderPaymentStatus


# ── Customer confirm received ─────────────────────────────────────────────────


class ConfirmReceivedRequest(BaseModel):
    """
    Customer visits the public tracking URL and confirms they received the order.

    Requires the secret tracking_code sent to them via SMS/WhatsApp at order creation.
    """

    tracking_code: str = Field(min_length=6, max_length=12)


# ── Structured address sub-schema (used in responses) ────────────────────────


class DeliveryAddressOut(BaseModel):
    """Structured delivery address returned in all order responses."""

    area: str
    block: str
    street: str
    building_no: str
    floor: str | None
    apartment: str | None
    city: str | None
    formatted: str  # human-readable single-line version


# ── Response schemas ──────────────────────────────────────────────────────────


class ShopOrderListItem(BaseModel):
    """Compact order summary for list views (staff and my-orders)."""

    id: uuid.UUID
    order_number: str
    customer_id: uuid.UUID
    customer_name: str
    customer_phone: str
    contact_number: str
    delivery_address: DeliveryAddressOut
    total_amount: Decimal
    currency: str
    delivery_status: str
    delivery_status_label: str
    delivery_status_label_ar: str
    payment_status: str
    items_count: int
    created_at: datetime
    updated_at: datetime


class ShopOrderDetailResponse(BaseModel):
    """Full order detail including items and delivery status history."""

    id: uuid.UUID
    order_number: str
    customer_id: uuid.UUID
    customer_name: str
    customer_phone: str
    contact_number: str
    delivery_address: DeliveryAddressOut
    delivery_notes: str | None
    subtotal: Decimal
    discount: Decimal
    total_amount: Decimal
    currency: str
    delivery_status: str
    delivery_status_label: str
    delivery_status_label_ar: str
    payment_status: str
    internal_notes: str | None
    items: list[OrderItemOut]
    status_history: list[StatusHistoryOut]
    created_at: datetime
    updated_at: datetime


class PublicOrderTrackingResponse(BaseModel):
    """
    Public order tracking view — deliberately excludes PII and internal fields.

    Accessible at GET /api/v1/shop/track/{order_number}/ without authentication.
    """

    order_number: str
    delivery_status: str
    delivery_status_label: str
    delivery_status_label_ar: str
    payment_status: str
    items: list[OrderItemOut]
    status_history: list[StatusHistoryOut]
    created_at: datetime


class ShopOrderListResponse(BaseModel):
    """Paginated list wrapper."""

    total: int
    limit: int
    offset: int
    results: list[ShopOrderListItem]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_address_out(order: Any) -> DeliveryAddressOut:
    return DeliveryAddressOut(
        area=order.area or "",
        block=order.block or "",
        street=order.street or "",
        building_no=order.building_no or "",
        floor=order.floor,
        apartment=order.apartment,
        city=order.city,
        formatted=order.formatted_address,
    )


def build_status_history_out(entry: Any) -> StatusHistoryOut:
    from_s = DeliveryStatus(entry.from_status)
    to_s = DeliveryStatus(entry.to_status)
    return StatusHistoryOut(
        from_status=entry.from_status,
        from_status_label=get_status_label(from_s, "en"),
        from_status_label_ar=get_status_label(from_s, "ar"),
        to_status=entry.to_status,
        to_status_label=get_status_label(to_s, "en"),
        to_status_label_ar=get_status_label(to_s, "ar"),
        changed_by=entry.changed_by,
        note=entry.note,
        created_at=entry.created_at,
    )


def order_to_list_item(order: Any) -> ShopOrderListItem:
    ds = DeliveryStatus(order.delivery_status)
    return ShopOrderListItem(
        id=order.id,
        order_number=order.order_number,
        customer_id=order.customer_id,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        contact_number=order.contact_number or "",
        delivery_address=_build_address_out(order),
        total_amount=order.total_amount,
        currency=order.currency,
        delivery_status=order.delivery_status,
        delivery_status_label=get_status_label(ds, "en"),
        delivery_status_label_ar=get_status_label(ds, "ar"),
        payment_status=order.payment_status,
        items_count=len(order.items),
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def order_to_detail(order: Any) -> ShopOrderDetailResponse:
    ds = DeliveryStatus(order.delivery_status)
    return ShopOrderDetailResponse(
        id=order.id,
        order_number=order.order_number,
        customer_id=order.customer_id,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        contact_number=order.contact_number or "",
        delivery_address=_build_address_out(order),
        delivery_notes=order.delivery_notes,
        subtotal=order.subtotal,
        discount=order.discount,
        total_amount=order.total_amount,
        currency=order.currency,
        delivery_status=order.delivery_status,
        delivery_status_label=get_status_label(ds, "en"),
        delivery_status_label_ar=get_status_label(ds, "ar"),
        payment_status=order.payment_status,
        internal_notes=order.internal_notes,
        items=[
            OrderItemOut(
                id=i.id,
                product_id=i.product_id,
                product_name=i.product_name,
                product_name_ar=i.product_name_ar,
                product_image_url=i.product_image_url,
                unit_price=i.unit_price,
                quantity=i.quantity,
                line_total=i.line_total,
                currency=i.currency,
            )
            for i in order.items
        ],
        status_history=[build_status_history_out(h) for h in order.status_history],
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def order_to_public_tracking(order: Any) -> PublicOrderTrackingResponse:
    ds = DeliveryStatus(order.delivery_status)
    return PublicOrderTrackingResponse(
        order_number=order.order_number,
        delivery_status=order.delivery_status,
        delivery_status_label=get_status_label(ds, "en"),
        delivery_status_label_ar=get_status_label(ds, "ar"),
        payment_status=order.payment_status,
        items=[
            OrderItemOut(
                id=i.id,
                product_id=i.product_id,
                product_name=i.product_name,
                product_name_ar=i.product_name_ar,
                product_image_url=i.product_image_url,
                unit_price=i.unit_price,
                quantity=i.quantity,
                line_total=i.line_total,
                currency=i.currency,
            )
            for i in order.items
        ],
        status_history=[build_status_history_out(h) for h in order.status_history],
        created_at=order.created_at,
    )
