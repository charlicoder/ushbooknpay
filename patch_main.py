import re

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/main.py', 'r') as f:
    content = f.read()

replacement = """from app.voucher.infrastructure.models import GiftVoucher  # noqa: F401
from app.gifts.infrastructure.models import (  # noqa: F401
    GiftVoucherCart,
    GiftVoucherCartItem,
    GiftVoucherPurchase,
    GiftVoucherPurchaseItem,
    GiftVoucherRecipient,
    GiftVoucherDelivery,
    GiftVoucherVerification,
    GiftVoucherRedemption,
    GiftVoucherStatusHistory,
)
"""

content = content.replace("from app.voucher.infrastructure.models import GiftVoucher  # noqa: F401\n", replacement)

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/main.py', 'w') as f:
    f.write(content)

