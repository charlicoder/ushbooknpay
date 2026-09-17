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
