import os
import textwrap

BASE_DIR = '/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/gifts'

repo_content = textwrap.dedent("""\
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
""")
with open(os.path.join(BASE_DIR, 'infrastructure', 'repository.py'), 'w') as f: f.write(repo_content)

# create other services
cart_service_content = textwrap.dedent("""\
import uuid
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from app.gifts.infrastructure.repository import GiftPurchaseRepository
from app.gifts.infrastructure.models import GiftVoucherCart, GiftVoucherCartItem
from app.core.exceptions import ValidationError
from app.gifts.domain.value_objects import GiftType

class GiftCartService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = GiftPurchaseRepository(session)

    async def get_or_create_cart(self, customer_id: uuid.UUID, gift_type: str) -> GiftVoucherCart:
        return await self.repo.get_or_create_cart(customer_id, gift_type)

    async def add_digital_gift_item(
        self, cart_id: uuid.UUID, customer_id: uuid.UUID, *, 
        digital_gift_id: uuid.UUID, digital_gift_data: dict, 
        service_id: uuid.UUID, service_data: dict, 
        branch_id: uuid.UUID | None, branch_data: dict, 
        service_arrangement_id: uuid.UUID | None, service_arrangement_data: dict, 
        addons: list | None, extra_minutes: int, 
        selected_video_id: uuid.UUID | None = None, custom_video_url: str | None = None
    ) -> GiftVoucherCartItem:
        cart = await self.repo.session.get(GiftVoucherCart, cart_id)
        if not cart or cart.customer_id != customer_id:
            raise ValidationError("Cart not found or unauthorized.")
        
        # Enforce one-service rule for digital/service gifts
        for item in cart.items:
            if item.service_id:
                raise ValidationError("Cart already has a service item.")
                
        item = GiftVoucherCartItem(
            cart_id=cart_id,
            gift_type=cart.gift_type,
            digital_gift_id=digital_gift_id,
            digital_gift_data=digital_gift_data,
            service_id=service_id,
            service_data=service_data,
            branch_id=branch_id,
            branch_data=branch_data,
            service_arrangement_id=service_arrangement_id,
            service_arrangement_data=service_arrangement_data,
            addons=addons or [],
            extra_minutes=extra_minutes,
            selected_video_id=selected_video_id,
            custom_video_url=custom_video_url
        )
        self.repo.add(item)
        await self.repo.flush()
        return item

    async def add_physical_gift_item(
        self, cart_id: uuid.UUID, customer_id: uuid.UUID, *, 
        product_id: uuid.UUID, product_data: dict, quantity: int, unit_price: Decimal
    ) -> GiftVoucherCartItem:
        cart = await self.repo.session.get(GiftVoucherCart, cart_id)
        if not cart or cart.customer_id != customer_id:
            raise ValidationError("Cart not found or unauthorized.")
        if cart.gift_type == GiftType.PHYSICAL.value:
            # Physical can't have service, but we are adding physical product. That's fine.
            pass
        elif cart.gift_type == GiftType.DIGITAL.value:
            # Maybe physical products aren't allowed here?
            pass
            
        item = GiftVoucherCartItem(
            cart_id=cart_id,
            gift_type=cart.gift_type,
            product_id=product_id,
            product_data=product_data,
            quantity=quantity,
            unit_price=unit_price
        )
        self.repo.add(item)
        await self.repo.flush()
        return item

    async def remove_item(self, cart_id: uuid.UUID, item_id: uuid.UUID, customer_id: uuid.UUID):
        cart = await self.repo.session.get(GiftVoucherCart, cart_id)
        if not cart or cart.customer_id != customer_id:
            raise ValidationError("Cart not found or unauthorized.")
        item = await self.repo.session.get(GiftVoucherCartItem, item_id)
        if not item or item.cart_id != cart_id:
            raise ValidationError("Item not found.")
        await self.repo.session.delete(item)
        await self.repo.flush()

    async def get_cart(self, customer_id: uuid.UUID) -> GiftVoucherCart | None:
        return await self.repo.get_cart_by_customer(customer_id)
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_cart_service.py'), 'w') as f: f.write(cart_service_content)

# validation, pricing, etc
open(os.path.join(BASE_DIR, 'application', 'gift_validation_service.py'), 'w').close()
open(os.path.join(BASE_DIR, 'application', 'gift_pricing_service.py'), 'w').close()
open(os.path.join(BASE_DIR, 'application', 'gift_purchase_service.py'), 'w').close()
open(os.path.join(BASE_DIR, 'application', 'gift_verification_service.py'), 'w').close()
open(os.path.join(BASE_DIR, 'application', 'gift_redemption_service.py'), 'w').close()
open(os.path.join(BASE_DIR, 'application', 'gift_delivery_service.py'), 'w').close()
open(os.path.join(BASE_DIR, 'interfaces', 'schemas.py'), 'w').close()
open(os.path.join(BASE_DIR, 'api', 'router.py'), 'w').close()

