import uuid
import secrets
import hashlib
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.gifts.infrastructure.models import GiftVoucherPurchase, GiftVoucherPurchaseItem, GiftVoucherRecipient, GiftVoucherStatusHistory
from app.gifts.domain.value_objects import GiftPurchaseStatus
from app.events.contracts import GiftPurchaseCompletedEvent

class GiftPurchaseService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_purchase(
        self, *, sender_id: uuid.UUID, sender_data: dict, cart, recipient_phone: str | None = None,
        recipient_id: uuid.UUID | None = None, recipient_data: dict | None = None,
        recipient_language: str = "ar", gift_message: str | None = None, gift_template: str | None = None,
        payment_id: str | None = None, payment_data: dict | None = None, payment_url: str | None = None,
        payment_provider: str | None = None, payment_through: str | None = None,
        status: str = GiftPurchaseStatus.PENDING_PAYMENT.value, created_by: uuid.UUID | None = None,
        expire_date: datetime | None = None
    ) -> tuple[GiftVoucherPurchase, str]:

        # calculate total amount
        from decimal import Decimal
        total_amount = Decimal("10.000") # mock

        secret_code_plaintext = f"{secrets.randbelow(1_000_000):06d}"
        secret_code_hash = hashlib.sha256(secret_code_plaintext.encode()).hexdigest()

        purchase = GiftVoucherPurchase(
            gift_type=cart.gift_type,
            sender_id=sender_id,
            sender_data=sender_data,
            recipient_phone=recipient_phone,
            recipient_id=recipient_id,
            recipient_data=recipient_data,
            recipient_language=recipient_language,
            total_amount=total_amount,
            gift_message=gift_message,
            gift_template=gift_template,
            secret_code_hash=secret_code_hash,
            status=status,
            payment_id=payment_id,
            payment_data=payment_data,
            payment_url=payment_url,
            payment_provider=payment_provider,
            payment_through=payment_through,
            created_by=created_by
        )
        if expire_date:
            purchase.expire_date = expire_date
        else:
            from app.gifts.infrastructure.models import _default_expire_date
            purchase.expire_date = _default_expire_date()

        self.session.add(purchase)

        # recipient record
        if recipient_phone or recipient_id:
            rec = GiftVoucherRecipient(purchase_id=purchase.id, phone_number=recipient_phone, language=recipient_language)
            self.session.add(rec)

        return purchase, secret_code_plaintext

    async def activate_purchase(
        self, purchase_id: uuid.UUID, *, payment_id: str | None = None, payment_data: dict | None = None,
        payment_provider: str | None = None, payment_through: str | None = None
    ) -> GiftVoucherPurchase:
        purchase = await self.session.get(GiftVoucherPurchase, purchase_id)
        purchase.status = GiftPurchaseStatus.ACTIVE.value
        purchase.payment_id = payment_id
        purchase.payment_data = payment_data
        purchase.payment_provider = payment_provider
        purchase.payment_through = payment_through
        self.session.add(GiftVoucherStatusHistory(purchase_id=purchase.id, to_status=purchase.status))
        return purchase

    async def update_purchase_status(self, purchase_id: uuid.UUID, new_status: str, changed_by=None, reason=None):
        purchase = await self.session.get(GiftVoucherPurchase, purchase_id)
        history = GiftVoucherStatusHistory(purchase_id=purchase.id, from_status=purchase.status, to_status=new_status, changed_by=changed_by, reason=reason)
        purchase.status = new_status
        self.session.add(history)
        return purchase
