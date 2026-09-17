import os
import textwrap

BASE_DIR = '/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/gifts'

validation_content = textwrap.dedent("""\
from app.core.exceptions import ValidationError
import logging
logger = logging.getLogger(__name__)

class GiftValidationService:
    def __init__(self, ushauth_client):
        self.client = ushauth_client

    async def validate_digital_gift_item(self, digital_gift_id, service_id, branch_id, service_arrangement_id, addons, extra_minutes) -> dict:
        try:
            # mock validation
            pass
        except Exception as e:
            logger.warning(f"Validation warning: {e}")
        return {"valid": True}

    async def validate_physical_gift_item(self, product_id, quantity) -> dict:
        try:
            pass
        except Exception as e:
            logger.warning(f"Validation warning: {e}")
        return {"valid": True}
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_validation_service.py'), 'w') as f: f.write(validation_content)


pricing_content = textwrap.dedent("""\
from decimal import Decimal
from app.gifts.infrastructure.models import GiftVoucherCart, GiftVoucherCartItem
from app.core.exceptions import ValidationError

class GiftPricingService:
    def calculate_cart_total(self, cart: GiftVoucherCart) -> Decimal:
        total = Decimal("0.000")
        for item in cart.items:
            total += self.calculate_item_subtotal(item)
        return total

    def calculate_item_subtotal(self, item: GiftVoucherCartItem) -> Decimal:
        # Mock calculation
        if item.gift_type == "PHYSICAL":
            return Decimal(item.unit_price or 0) * item.quantity
        return Decimal("10.000") # mock price for DIGITAL / SERVICE

    def validate_amount(self, claimed_amount: Decimal, calculated_amount: Decimal):
        if abs(claimed_amount - calculated_amount) > Decimal("0.001"):
            raise ValidationError("Claimed amount differs from calculated amount")
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_pricing_service.py'), 'w') as f: f.write(pricing_content)


purchase_content = textwrap.dedent("""\
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
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_purchase_service.py'), 'w') as f: f.write(purchase_content)


verification_content = textwrap.dedent("""\
import hashlib
from datetime import datetime, timedelta, timezone
from app.core.exceptions import ValidationError
from app.gifts.infrastructure.models import GiftVoucherPurchase, GiftVoucherVerification
from app.gifts.domain.value_objects import GiftPurchaseStatus

class GiftVerificationService:
    MAX_ATTEMPTS = 5
    LOCKOUT_MINUTES = 15

    async def verify_secret_code(self, purchase: GiftVoucherPurchase, submitted_code: str, verification_record: GiftVoucherVerification) -> bool:
        if purchase.status not in (GiftPurchaseStatus.ACTIVE.value, GiftPurchaseStatus.CLAIMED.value):
            raise ValidationError("Purchase is not active or claimed.")
        if verification_record.locked_until and verification_record.locked_until > datetime.now(timezone.utc):
            raise ValidationError("Verification locked.")
            
        submitted_hash = hashlib.sha256(submitted_code.encode()).hexdigest()
        if submitted_hash == purchase.secret_code_hash:
            purchase.status = GiftPurchaseStatus.CLAIMED.value
            return True
            
        verification_record.attempt_count += 1
        verification_record.last_attempt_at = datetime.now(timezone.utc)
        if verification_record.attempt_count >= self.MAX_ATTEMPTS:
            verification_record.locked_until = datetime.now(timezone.utc) + timedelta(minutes=self.LOCKOUT_MINUTES)
        return False
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_verification_service.py'), 'w') as f: f.write(verification_content)


redemption_content = textwrap.dedent("""\
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
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_redemption_service.py'), 'w') as f: f.write(redemption_content)


delivery_content = textwrap.dedent("""\
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
""")
with open(os.path.join(BASE_DIR, 'application', 'gift_delivery_service.py'), 'w') as f: f.write(delivery_content)


schemas_content = textwrap.dedent("""\
from pydantic import BaseModel, ConfigDict
from typing import Any
import uuid
from datetime import datetime

class CreateGiftCartRequest(BaseModel):
    gift_type: str

class AddDigitalGiftItemRequest(BaseModel):
    digital_gift_id: uuid.UUID
    service_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    service_arrangement_id: uuid.UUID | None = None
    addons: list | None = None
    extra_minutes: int = 0
    selected_video_id: uuid.UUID | None = None
    custom_video_url: str | None = None

class AddPhysicalGiftItemRequest(BaseModel):
    product_id: uuid.UUID
    quantity: int

class CreateGiftPurchaseRequest(BaseModel):
    cart_id: uuid.UUID
    recipient_phone: str | None = None
    recipient_id: uuid.UUID | None = None
    recipient_name: str | None = None
    recipient_language: str = 'ar'
    gift_message: str | None = None
    gift_template: str | None = None
    payment_id: str | None = None
    payment_data: dict | None = None
    payment_url: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None
    status: str | None = None
    expire_date: datetime | None = None

class VerifyGiftCodeRequest(BaseModel):
    secret_code: str

class RedeemGiftRequest(BaseModel):
    booking_id: uuid.UUID | None = None
    note: str | None = None

class UpdateGiftStatusRequest(BaseModel):
    status: str
    reason: str | None = None

class GiftPurchaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    public_token: str
    status: str
    gift_type: str
    total_amount: float
    expire_date: datetime

class GiftPublicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    public_token: str
    status: str

class GiftPurchaseListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
""")
with open(os.path.join(BASE_DIR, 'interfaces', 'schemas.py'), 'w') as f: f.write(schemas_content)


router_content = textwrap.dedent("""\
from fastapi import APIRouter
router = APIRouter(prefix="/gifts", tags=["Gift Vouchers V2"])

@router.post("/cart/")
async def create_cart(): return {"success": True}

@router.get("/cart/")
async def get_cart(): return {"success": True}

@router.post("/cart/items/digital/")
async def add_digital_item(): return {"success": True}

@router.post("/cart/items/physical/")
async def add_physical_item(): return {"success": True}

@router.delete("/cart/items/{item_id}/")
async def delete_item(item_id: str): return {"success": True}

@router.post("/purchases/")
async def create_purchase(): return {"success": True}

@router.get("/purchases/")
async def list_purchases(): return {"success": True}

@router.get("/purchases/{purchase_id}/")
async def get_purchase(purchase_id: str): return {"success": True}

@router.patch("/purchases/{purchase_id}/status/")
async def update_status(purchase_id: str): return {"success": True}

@router.get("/public/{token}/")
async def public_gift(token: str): return {"success": True}

@router.post("/public/{token}/verify/")
async def verify_gift(token: str): return {"success": True}

@router.post("/{purchase_id}/redeem/")
async def redeem_gift(purchase_id: str): return {"success": True}

@router.post("/{purchase_id}/delivery/confirm-receipt/")
async def confirm_receipt(purchase_id: str): return {"success": True}
""")
with open(os.path.join(BASE_DIR, 'api', 'router.py'), 'w') as f: f.write(router_content)

