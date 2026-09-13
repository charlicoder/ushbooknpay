"""
app/shop/infrastructure/repository.py
───────────────────────────────────────
ShopOrderRepository — async persistence layer for the Shop domain.

Responsibilities:
- CRUD for ShopOrder, ShopOrderItem, ShopOrderStatusHistory
- Filtered paginated list queries
- No business logic — only data access
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.shop.infrastructure.models import (
    ShopOrder,
    ShopOrderItem,
    ShopOrderStatusHistory,
)

logger = get_logger(__name__)


class ShopOrderNotFoundError(NotFoundError):
    default_message = "Shop order not found."


class ShopOrderRepository:
    """Async repository for the ShopOrder aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────

    async def create(self, order: ShopOrder) -> ShopOrder:
        """Persist a new order with its items. Does NOT commit — caller manages tx."""
        self._session.add(order)
        await self._session.flush()
        await self._session.refresh(order)
        logger.info("shop_order_created", order_id=str(order.id), order_number=order.order_number)
        return order

    async def add_status_history(self, entry: ShopOrderStatusHistory) -> None:
        """Append a status history record within the current transaction."""
        self._session.add(entry)
        await self._session.flush()

    # ── Read ─────────────────────────────────────────────────────────────

    async def get_by_id(self, order_id: uuid.UUID) -> ShopOrder:
        """Return a ShopOrder by primary key, with items and history eager-loaded."""
        stmt = (
            select(ShopOrder)
            .where(ShopOrder.id == order_id)
            .options(
                selectinload(ShopOrder.items),
                selectinload(ShopOrder.status_history),
            )
        )
        result = await self._session.execute(stmt)
        order = result.scalar_one_or_none()
        if order is None:
            raise ShopOrderNotFoundError(f"ShopOrder id={order_id} not found.")
        return order

    async def get_by_order_number(self, order_number: str) -> ShopOrder:
        """Return a ShopOrder by its human-readable order number."""
        stmt = (
            select(ShopOrder)
            .where(ShopOrder.order_number == order_number)
            .options(
                selectinload(ShopOrder.items),
                selectinload(ShopOrder.status_history),
            )
        )
        result = await self._session.execute(stmt)
        order = result.scalar_one_or_none()
        if order is None:
            raise ShopOrderNotFoundError(f"ShopOrder number={order_number} not found.")
        return order

    async def list_orders(
        self,
        *,
        customer_id: uuid.UUID | None = None,
        delivery_status: str | None = None,
        payment_status: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ShopOrder], int]:
        """
        Return a paginated list of orders with optional filters.

        Returns (items, total_count).
        """
        base = select(ShopOrder).options(
            selectinload(ShopOrder.items),
            selectinload(ShopOrder.status_history),
        )
        count_base = select(func.count(ShopOrder.id))

        if customer_id is not None:
            base = base.where(ShopOrder.customer_id == customer_id)
            count_base = count_base.where(ShopOrder.customer_id == customer_id)
        if delivery_status:
            base = base.where(ShopOrder.delivery_status == delivery_status)
            count_base = count_base.where(ShopOrder.delivery_status == delivery_status)
        if payment_status:
            base = base.where(ShopOrder.payment_status == payment_status)
            count_base = count_base.where(ShopOrder.payment_status == payment_status)
        if from_date:
            base = base.where(ShopOrder.created_at >= from_date)
            count_base = count_base.where(ShopOrder.created_at >= from_date)
        if to_date:
            base = base.where(ShopOrder.created_at <= to_date)
            count_base = count_base.where(ShopOrder.created_at <= to_date)
        if search:
            pattern = f"%{search}%"
            base = base.where(
                ShopOrder.order_number.ilike(pattern)
                | ShopOrder.customer_name.ilike(pattern)
                | ShopOrder.customer_phone.ilike(pattern)
            )
            count_base = count_base.where(
                ShopOrder.order_number.ilike(pattern)
                | ShopOrder.customer_name.ilike(pattern)
                | ShopOrder.customer_phone.ilike(pattern)
            )

        count_result = await self._session.execute(count_base)
        total = count_result.scalar_one()

        stmt = base.order_by(ShopOrder.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        orders = list(result.scalars().all())
        return orders, total

    # ── Update ────────────────────────────────────────────────────────────

    async def update(self, order: ShopOrder) -> ShopOrder:
        """Flush changes to an existing order. Does NOT commit."""
        await self._session.flush()
        await self._session.refresh(order)
        return order

    # ── Helpers ───────────────────────────────────────────────────────────

    async def next_order_number(self) -> str:
        """
        Generate the next sequential order number in the format ORD-YYYY-NNNN.

        Uses a DB count query so it's race-safe within a serializable transaction.
        """
        from datetime import date

        year = date.today().year
        stmt = select(func.count(ShopOrder.id)).where(
            func.extract("year", ShopOrder.created_at) == year
        )
        result = await self._session.execute(stmt)
        count = result.scalar_one() or 0
        return f"ORD-{year}-{count + 1:04d}"
