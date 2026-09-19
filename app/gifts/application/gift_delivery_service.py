import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.gifts.infrastructure.models import GiftVoucherDelivery
from app.gifts.domain.value_objects import GiftDeliveryStatus

class GiftDeliveryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_delivery(self, purchase_id: uuid.UUID, *, address: dict) -> GiftVoucherDelivery:
        d = GiftVoucherDelivery(purchase_id=purchase_id, address=address)
        self.session.add(d)
        return d

    async def update_delivery_status(self, purchase_id: uuid.UUID, new_status: str, *, tracking_reference: str | None = None, delivered_at: datetime | None = None, received_at: datetime | None = None) -> GiftVoucherDelivery:
        # fetch and update...
        return GiftVoucherDelivery()

    async def confirm_receipt(self, purchase_id: uuid.UUID, recipient_id: uuid.UUID) -> GiftVoucherDelivery:
        return GiftVoucherDelivery()
