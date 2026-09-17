import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.gifts.infrastructure.models import (
    GiftVoucherCart,
    GiftVoucherPurchase,
    GiftVoucherVerification,
)
from app.gifts.domain.value_objects import GiftCartStatus

class GiftPurchaseRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, purchase_id: uuid.UUID) -> GiftVoucherPurchase | None:
        stmt = select(GiftVoucherPurchase).where(GiftVoucherPurchase.id == purchase_id).options(
            selectinload(GiftVoucherPurchase.items),
            selectinload(GiftVoucherPurchase.recipient_record),
            selectinload(GiftVoucherPurchase.verification)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_public_token(self, token: str) -> GiftVoucherPurchase | None:
        stmt = select(GiftVoucherPurchase).where(GiftVoucherPurchase.public_token == token).options(
            selectinload(GiftVoucherPurchase.items),
            selectinload(GiftVoucherPurchase.recipient_record)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_cart_by_customer(self, customer_id: uuid.UUID) -> GiftVoucherCart | None:
        stmt = select(GiftVoucherCart).where(
            GiftVoucherCart.customer_id == customer_id,
            GiftVoucherCart.status == GiftCartStatus.ACTIVE.value
        ).options(selectinload(GiftVoucherCart.items))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    def add(self, entity: Any) -> None:
        self.session.add(entity)

    async def flush(self) -> None:
        await self.session.flush()

    async def list_by_sender(self, sender_id: uuid.UUID, status: str | None = None, page: int = 1, page_size: int = 20) -> tuple[list[GiftVoucherPurchase], int]:
        stmt = select(GiftVoucherPurchase).where(GiftVoucherPurchase.sender_id == sender_id)
        if status:
            stmt = stmt.where(GiftVoucherPurchase.status == status)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = await self.session.scalar(count_stmt)

        stmt = stmt.limit(page_size).offset((page - 1) * page_size).options(
            selectinload(GiftVoucherPurchase.items),
            selectinload(GiftVoucherPurchase.recipient_record)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all(), total or 0

    async def list_by_recipient_phone(self, phone: str, status: str | None = None, page: int = 1, page_size: int = 20) -> tuple[list[GiftVoucherPurchase], int]:
        stmt = select(GiftVoucherPurchase).where(GiftVoucherPurchase.recipient_phone == phone)
        if status:
            stmt = stmt.where(GiftVoucherPurchase.status == status)

        from sqlalchemy import func
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = await self.session.scalar(count_stmt)

        stmt = stmt.limit(page_size).offset((page - 1) * page_size).options(
            selectinload(GiftVoucherPurchase.items),
            selectinload(GiftVoucherPurchase.recipient_record)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all(), total or 0

    async def get_or_create_cart(self, customer_id: uuid.UUID, gift_type: str) -> GiftVoucherCart:
        cart = await self.get_cart_by_customer(customer_id)
        if not cart:
            cart = GiftVoucherCart(customer_id=customer_id, gift_type=gift_type)
            self.add(cart)
            await self.flush()
        return cart

    async def get_verification(self, purchase_id: uuid.UUID) -> GiftVoucherVerification | None:
        stmt = select(GiftVoucherVerification).where(GiftVoucherVerification.purchase_id == purchase_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_or_create_verification(self, purchase_id: uuid.UUID) -> GiftVoucherVerification:
        v = await self.get_verification(purchase_id)
        if not v:
            v = GiftVoucherVerification(purchase_id=purchase_id)
            self.add(v)
            await self.flush()
        return v
