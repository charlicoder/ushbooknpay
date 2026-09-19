import re

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/events/contracts.py', 'r') as f:
    content = f.read()

# Update BaseEvent.event_type
pattern = r'(elif name == "Voucher.Redeemed":\n\s+return "voucher.redeemed"\n)'
replacement = r'\1        elif name == "Gift.Purchase.Completed":\n            return "gift.purchase.completed"\n        elif name == "Gift.Claimed":\n            return "gift.claimed"\n        elif name == "Gift.Redeemed":\n            return "gift.redeemed"\n        elif name == "Gift.Delivered":\n            return "gift.delivered"\n'
content = re.sub(pattern, replacement, content)

# Append new events at the end
new_events = """
# ── Gift Purchase Events (V2) ─────────────────────────────────────────────────

@dataclass
class GiftPurchaseCompletedEvent(BaseEvent):
    \"\"\"
    Fired when a Gift Voucher V2 purchase is successfully activated (payment confirmed).
    \"\"\"
    event_name: str = field(default="Gift.Purchase.Completed", init=False)
    event_type: str = field(default="gift.purchase.completed", init=False)

    id: str = ""
    gift_type: str = ""  
    status: str = "ACTIVE"
    public_token: str = ""
    secret_code: str = ""  

    digital_gift_id: str | None = None
    digital_gift_data: dict[str, Any] = field(default_factory=dict)

    total_amount: str = ""
    currency: str = "KWD"
    expire_date: str | None = None

    gift_message: str | None = None
    gift_template: str | None = None

    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)

    recipient_phone: str | None = None
    recipient_id: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    recipient_language: str = "ar"  

    payment_id: str | None = None
    payment_provider: str | None = None
    payment_through: str | None = None

    created_at: str | None = None


@dataclass
class GiftClaimedEvent(BaseEvent):
    \"\"\"
    Fired when a recipient successfully verifies the secret code (gift.claimed).
    \"\"\"
    event_name: str = field(default="Gift.Claimed", init=False)
    event_type: str = field(default="gift.claimed", init=False)

    id: str = ""
    gift_type: str = ""
    public_token: str = ""
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    claimed_at: str | None = None


@dataclass
class GiftRedeemedPurchaseEvent(BaseEvent):
    \"\"\"
    Fired when a Gift Purchase V2 is redeemed.
    \"\"\"
    event_name: str = field(default="Gift.Redeemed", init=False)
    event_type: str = field(default="gift.redeemed", init=False)

    id: str = ""
    gift_type: str = ""
    public_token: str = ""
    total_amount: str = ""
    currency: str = "KWD"
    sender_id: str = ""
    sender_data: dict[str, Any] = field(default_factory=dict)
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    recipient_language: str = "ar"
    redeemed_by: str | None = None
    booking_id: str | None = None
    redeemed_at: str | None = None


@dataclass
class GiftDeliveredEvent(BaseEvent):
    \"\"\"
    Fired when a Physical Gift delivery status reaches DELIVERED.
    \"\"\"
    event_name: str = field(default="Gift.Delivered", init=False)
    event_type: str = field(default="gift.delivered", init=False)

    id: str = ""
    gift_type: str = "PHYSICAL"
    public_token: str = ""
    recipient_phone: str | None = None
    recipient_data: dict[str, Any] = field(default_factory=dict)
    recipient_language: str = "ar"
    tracking_reference: str | None = None
    delivered_at: str | None = None
"""
content += new_events

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/events/contracts.py', 'w') as f:
    f.write(content)
