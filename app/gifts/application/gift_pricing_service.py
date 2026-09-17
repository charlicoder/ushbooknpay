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
