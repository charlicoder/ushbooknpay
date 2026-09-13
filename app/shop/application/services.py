"""
app/shop/application/services.py
──────────────────────────────────
ShopOrderService — orchestrates the shop domain.

Use cases:
1. create_order           — validate products, snapshot prices, persist, fire SQS event
2. update_delivery_status — staff advances the delivery state
3. update_payment_status  — ushdesk / mobile marks order paid/failed
4. confirm_received       — customer confirms receipt using tracking code
5. get_order              — detail retrieval
6. list_orders            — paginated filtered list
7. get_my_orders          — customer's own orders
"""

from __future__ import annotations

import asyncio
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError, ValidationError, AuthorizationError
from app.shop.domain.state_machine import (
    DeliveryStateMachine,
    InvalidDeliveryTransitionError,
)
from app.shop.domain.value_objects import DeliveryStatus, OrderPaymentStatus
from app.shop.infrastructure.models import (
    ShopOrder,
    ShopOrderItem,
    ShopOrderStatusHistory,
)
from app.shop.infrastructure.repository import ShopOrderRepository, ShopOrderNotFoundError
from app.shop.interfaces.schemas import CreateShopOrderRequest

logger = structlog.get_logger(__name__)

_TRACKING_CODE_RNG = random.SystemRandom()  # cryptographically seeded


def _generate_tracking_code() -> str:
    """Return a random 6-digit numeric PIN (100000–999999)."""
    return str(_TRACKING_CODE_RNG.randint(100000, 999999))


def _generate_public_token() -> str:
    """Return a URL-safe random 43-character token (32 bytes base64url, no padding)."""
    return secrets.token_urlsafe(32)


def _token_expiry(weeks: int = 1) -> datetime:
    """Return a UTC datetime ``weeks`` from now."""
    return datetime.now(timezone.utc) + timedelta(weeks=weeks)


class ShopOrderService:
    """
    Application service for the Shop domain.

    Depends on:
      - ShopOrderRepository (database)
      - UshAuthClient (product catalogue)
      - SQSClient (events)
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        ushauth_client: Any = None,
    ) -> None:
        self._session = session
        self._repo = ShopOrderRepository(session)
        self._settings = settings or get_settings()
        self._ushauth = ushauth_client  # injected from router via USHAuthDep

    # ── 1. Create order ───────────────────────────────────────────────────

    async def create_order(
        self,
        body: CreateShopOrderRequest,
        customer_id: uuid.UUID,
        customer_name: str,
        customer_phone: str,
    ) -> ShopOrder:
        """
        Validate products against ushauth, snapshot prices, persist the order,
        and fire a ShopOrderCreatedEvent to SQS.
        """
        # ── 1. Fetch product data from ushauth ────────────────────────────
        product_map: dict[uuid.UUID, dict[str, Any]] = {}
        for item_req in body.items:
            try:
                product = await self._ushauth.get_product(str(item_req.product_id))
                product_map[item_req.product_id] = product
            except Exception as exc:
                raise ValidationError(
                    f"Product {item_req.product_id} not found or unavailable: {exc}"
                )

        # ── 2. Build line items and compute totals ────────────────────────
        order_items: list[ShopOrderItem] = []
        subtotal = Decimal("0.000")

        for item_req in body.items:
            product = product_map[item_req.product_id]
            unit_price = Decimal(str(product.get("price", "0")))
            qty = item_req.quantity
            line_total = (unit_price * qty).quantize(Decimal("0.001"))
            subtotal += line_total

            # Snapshot image1 as the primary display image (may be a full URL or relative path)
            image_url: str | None = (
                product.get("image1") or product.get("image") or None
            )

            order_items.append(
                ShopOrderItem(
                    id=uuid.uuid4(),
                    product_id=item_req.product_id,
                    product_name=product.get("name") or product.get("name_en") or "",
                    product_name_ar=product.get("name_ar") or "",
                    product_image_url=image_url,
                    unit_price=unit_price,
                    quantity=qty,
                    line_total=line_total,
                    currency="KWD",
                )
            )

        total_amount = subtotal  # no discount at creation time

        # ── 3. Generate order number, tokens ──────────────────────────────
        order_number = await self._repo.next_order_number()
        tracking_code = _generate_tracking_code()   # 6-digit PIN
        public_token = _generate_public_token()      # URL-safe random string
        token_expires_at = _token_expiry(weeks=1)    # expires in 1 week

        # ── 4. Persist ────────────────────────────────────────────────────
        order = ShopOrder(
            id=uuid.uuid4(),
            order_number=order_number,
            customer_id=customer_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            contact_number=body.contact_number or customer_phone or "",
            # Structured address fields
            area=body.area or "",
            block=body.block or "",
            street=body.street or "",
            building_no=body.building_no or "",
            floor=body.floor,
            apartment=body.apartment,
            city=body.city,
            delivery_notes=body.delivery_notes,
            delivery_status=DeliveryStatus.ORDERED.value,
            tracking_code=tracking_code,
            public_token=public_token,
            token_expires_at=token_expires_at,
            subtotal=subtotal,
            discount=Decimal("0.000"),
            total_amount=total_amount,
            currency="KWD",
            payment_status=OrderPaymentStatus.NOT_INITIATED.value,
            internal_notes=body.internal_notes,
            items=order_items,
        )
        order = await self._repo.create(order)

        # Initial status history entry
        await self._repo.add_status_history(
            ShopOrderStatusHistory(
                id=uuid.uuid4(),
                order_id=order.id,
                from_status=DeliveryStatus.ORDERED.value,
                to_status=DeliveryStatus.ORDERED.value,
                changed_by="system",
                note="Order placed",
            )
        )
        await self._session.commit()
        await self._session.refresh(order)

        # ── 5. Fire SQS event only if already paid (e.g. ushdesk cash orders) ──
        if order.payment_status == OrderPaymentStatus.SUCCESS.value:
            self._enqueue_order_created_event(order)

        logger.info(
            "shop_order_created",
            order_id=str(order.id),
            order_number=order.order_number,
            customer_id=str(customer_id),
            total_amount=str(total_amount),
            payment_status=order.payment_status,
        )
        return order

    # ── 2. Update delivery status (staff) ────────────────────────────────

    async def update_delivery_status(
        self,
        order_id: uuid.UUID,
        new_status: DeliveryStatus,
        changed_by: str,
        note: str | None = None,
    ) -> ShopOrder:
        """
        Advance the delivery status.  Only staff transitions allowed here
        (delivered → received requires confirm_received).
        """
        order = await self._repo.get_by_id(order_id)
        machine = DeliveryStateMachine(DeliveryStatus(order.delivery_status))

        try:
            machine.assert_staff_can_transition(new_status)
        except InvalidDeliveryTransitionError as exc:
            raise ValidationError(str(exc))

        old_status = order.delivery_status
        order.delivery_status = new_status.value
        await self._repo.update(order)
        await self._repo.add_status_history(
            ShopOrderStatusHistory(
                id=uuid.uuid4(),
                order_id=order.id,
                from_status=old_status,
                to_status=new_status.value,
                changed_by=changed_by,
                note=note,
            )
        )
        await self._session.commit()
        await self._session.refresh(order)
        logger.info(
            "shop_delivery_status_updated",
            order_id=str(order.id),
            from_status=old_status,
            to_status=new_status.value,
            changed_by=changed_by,
        )
        return order

    # ── 3. Update payment status ──────────────────────────────────────────

    async def update_payment_status(
        self,
        order_id: uuid.UUID,
        new_payment_status: OrderPaymentStatus,
        changed_by: str,
        payment_method: str | None = None,
        payment_type: str | None = None,
        payment_provider: str | None = None,
    ) -> ShopOrder:
        """Update payment_status — called by ushdesk (mark paid) or mobile gateway callback.

        Also stores payment classification fields (method/type/provider) so that
        the SQS ShopOrderCreatedEvent carries enough data for ushnotice to create
        a payment record without a second DB lookup.

        Fires a ShopOrderCreatedEvent to SQS when the status transitions to ``success``.
        """
        order = await self._repo.get_by_id(order_id)
        previous_payment_status = order.payment_status
        order.payment_status = new_payment_status.value

        # Store classification when provided (overwrite if already set)
        if payment_method is not None:
            order.payment_method = payment_method
        if payment_type is not None:
            order.payment_type = payment_type
        if payment_provider is not None:
            order.payment_provider = payment_provider

        await self._repo.update(order)
        await self._session.commit()
        await self._session.refresh(order)
        logger.info(
            "shop_payment_status_updated",
            order_id=str(order.id),
            order_number=order.order_number,
            from_payment_status=previous_payment_status,
            to_payment_status=new_payment_status.value,
            payment_method=payment_method,
            payment_type=payment_type,
            changed_by=changed_by,
        )

        # Fire SQS event when payment transitions to success (mobile or ushdesk)
        if new_payment_status == OrderPaymentStatus.SUCCESS:
            self._enqueue_order_created_event(order)
            logger.info(
                "shop_order_paid_event_enqueued",
                order_id=str(order.id),
                order_number=order.order_number,
            )

        return order

    # ── 4. Customer confirms received ─────────────────────────────────────

    async def confirm_received(
        self,
        public_token: str,
        tracking_code: str,
    ) -> ShopOrder:
        """
        Customer visits the public tracking URL and confirms receipt.

        Validates:
        1. Order exists (looked up by public_token)
        2. Token has not expired (token_expires_at)
        3. tracking_code (6-digit PIN) matches the order's secret code
        4. Current delivery status is 'delivered'
        """
        import hmac as _hmac

        order = await self._repo.get_by_public_token(public_token)

        # Check token/code expiry
        if order.token_expires_at is not None:
            now = datetime.now(timezone.utc)
            expires = order.token_expires_at
            # Make offset-aware if stored as naive UTC
            if expires.tzinfo is None:
                from datetime import timezone as _tz
                expires = expires.replace(tzinfo=_tz.utc)
            if now > expires:
                raise AuthorizationError("Tracking link has expired.")

        # Validate 6-digit PIN (constant-time compare)
        if not _hmac.compare_digest(order.tracking_code, tracking_code):
            raise AuthorizationError("Invalid tracking code.")

        machine = DeliveryStateMachine(DeliveryStatus(order.delivery_status))
        try:
            machine.assert_customer_can_transition(DeliveryStatus.RECEIVED)
        except InvalidDeliveryTransitionError as exc:
            raise ValidationError(str(exc))

        old_status = order.delivery_status
        order.delivery_status = DeliveryStatus.RECEIVED.value
        await self._repo.update(order)
        await self._repo.add_status_history(
            ShopOrderStatusHistory(
                id=uuid.uuid4(),
                order_id=order.id,
                from_status=old_status,
                to_status=DeliveryStatus.RECEIVED.value,
                changed_by="customer",
                note="Customer confirmed receipt via tracking URL",
            )
        )
        await self._session.commit()
        await self._session.refresh(order)
        logger.info(
            "shop_order_received",
            order_id=str(order.id),
            order_number=order.order_number,
        )
        return order

    # ── 5. Get order detail ───────────────────────────────────────────────

    async def get_order(self, order_id: uuid.UUID) -> ShopOrder:
        return await self._repo.get_by_id(order_id)

    async def get_order_by_number(self, order_number: str) -> ShopOrder:
        return await self._repo.get_by_order_number(order_number)

    async def get_order_by_public_token(self, public_token: str) -> ShopOrder:
        return await self._repo.get_by_public_token(public_token)

    # ── 6. List orders (staff) ────────────────────────────────────────────

    async def list_orders(
        self,
        *,
        delivery_status: str | None = None,
        payment_status: str | None = None,
        from_date: Any | None = None,
        to_date: Any | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ShopOrder], int]:
        return await self._repo.list_orders(
            delivery_status=delivery_status,
            payment_status=payment_status,
            from_date=from_date,
            to_date=to_date,
            search=search,
            limit=limit,
            offset=offset,
        )

    # ── 7. My orders (customer) ───────────────────────────────────────────

    async def get_my_orders(
        self,
        customer_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ShopOrder], int]:
        return await self._repo.list_orders(
            customer_id=customer_id,
            limit=limit,
            offset=offset,
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _enqueue_order_created_event(self, order: ShopOrder) -> None:
        """Fire-and-forget SQS publish of ShopOrderCreatedEvent."""
        from app.events.contracts import ShopOrderCreatedEvent
        from app.events.sqs_client import get_sqs_client

        event = ShopOrderCreatedEvent(
            # ── Identity ─────────────────────────────────────────────
            order_id=str(order.id),
            order_number=order.order_number,
            # ── Customer ─────────────────────────────────────────────
            customer_id=str(order.customer_id),
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            customer_data={
                "id": str(order.customer_id),
                "name": order.customer_name,
                "phone": order.customer_phone,
                "contact_number": order.contact_number or order.customer_phone,
            },
            # ── Delivery ─────────────────────────────────────────────
            delivery_address=order.formatted_address,
            # ── Tracking ─────────────────────────────────────────────
            public_token=order.public_token,
            tracking_code=order.tracking_code,
            # ── Financials ───────────────────────────────────────────
            subtotal=str(order.subtotal),
            total_amount=str(order.total_amount),
            currency=order.currency,
            # ── Payment classification ────────────────────────────────
            payment_status=order.payment_status,           # always "success" at this point
            payment_method=order.payment_method or "",     # e.g. "card", "cash", "knet"
            payment_type=order.payment_type or "",         # e.g. "gateway", "desk"
            payment_provider=order.payment_provider or "", # e.g. "MyFatoorah", "DirectLink"
            # ── Items snapshot ────────────────────────────────────────
            items=[
                {
                    "product_id": str(i.product_id),
                    "product_name": i.product_name,
                    "product_name_ar": i.product_name_ar,
                    "quantity": i.quantity,
                    "unit_price": str(i.unit_price),
                    "line_total": str(i.line_total),
                    "product_image_url": i.product_image_url,
                }
                for i in order.items
            ],
        )
        try:
            sqs = get_sqs_client()
            asyncio.create_task(sqs.publish_event(event))
        except Exception as exc:
            logger.error("shop_event_publish_failed", error=str(exc))
