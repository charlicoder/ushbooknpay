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
