import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.gifts.infrastructure.models import GiftVoucherPurchase, GiftVoucherRedemption
from app.gifts.domain.value_objects import GiftPurchaseStatus

class GiftRedemptionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def redeem(self, purchase_id: uuid.UUID, *, redeemed_by: uuid.UUID | None = None, booking_id: uuid.UUID | None = None, booking_data: dict | None = None, note: str | None = None) -> GiftVoucherRedemption:
        stmt = select(GiftVoucherPurchase).where(GiftVoucherPurchase.id == purchase_id).with_for_update(skip_locked=True)
        result = await self.session.execute(stmt)
        purchase = result.scalars().first()

        redemption = GiftVoucherRedemption(
            purchase_id=purchase_id,
            redeemed_by=redeemed_by,
            booking_id=booking_id,
            booking_data=booking_data,
            note=note
        )
        purchase.status = GiftPurchaseStatus.REDEEMED.value
        self.session.add(redemption)
        return redemption
