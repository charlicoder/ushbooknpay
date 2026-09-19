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
