"""
app/loyalty/infrastructure/repository.py
──────────────────────────────────────────
Async SQLAlchemy repository for the loyalty programme.

All writes are atomic within the caller's session transaction.
The session is managed by the FastAPI dependency layer — this
repository never commits directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.loyalty.domain.models import LoyaltyAccountDomain, LoyaltyTransactionDomain, TransactionType
from app.loyalty.infrastructure.models import LoyaltyAccount, LoyaltyTransaction

logger = structlog.get_logger(__name__)


class LoyaltyRepository:
    """Async data-access layer for loyalty_account and loyalty_transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Account queries ────────────────────────────────────────────────────────

    async def get_account_by_customer(
        self, customer_id: uuid.UUID
    ) -> LoyaltyAccount | None:
        """Return the loyalty account for a customer, or None if it doesn't exist yet."""
        result = await self._session.execute(
            select(LoyaltyAccount).where(LoyaltyAccount.customer_id == customer_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create_account(
        self, customer_id: uuid.UUID
    ) -> tuple[LoyaltyAccount, bool]:
        """
        Fetch or create a loyalty account for the customer.

        Returns:
            (account, created) — ``created`` is True when a new record was inserted.
        """
        account = await self.get_account_by_customer(customer_id)
        if account is not None:
            return account, False

        account = LoyaltyAccount(customer_id=customer_id)
        self._session.add(account)
        await self._session.flush()  # populate id / defaults without committing
        logger.info("loyalty_account_created", customer_id=str(customer_id))
        return account, True

    # ── Writes ────────────────────────────────────────────────────────────────

    async def credit_points(
        self,
        customer_id: uuid.UUID,
        points: int,
        expiry: datetime,
        *,
        booking_id: uuid.UUID | None = None,
        booking_number: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
    ) -> LoyaltyAccount:
        """
        Add ``points`` to the customer's balance and reset the expiry window.

        Creates the account if it doesn't exist yet.

        Args:
            customer_id:    Customer UUID.
            points:         Points to credit (must be > 0).
            expiry:         New ``points_expire_at`` value (now + EXPIRY_DAYS).
            booking_id:     Associated booking UUID (optional).
            booking_number: Human-readable booking reference (optional).
            description:    Human-readable note for the transaction log.
            created_by:     Source identifier (e.g. 'ushnotice', 'admin').

        Returns:
            Updated LoyaltyAccount ORM instance.
        """
        if points <= 0:
            raise ValueError(f"Points to credit must be > 0, got {points}.")

        account, _ = await self.get_or_create_account(customer_id)

        account.balance_points += points
        account.total_earned += points
        account.points_expire_at = expiry
        account.updated_at = datetime.utcnow()

        txn = LoyaltyTransaction(
            account_id=account.id,
            customer_id=customer_id,
            transaction_type=TransactionType.EARN.value,
            points=points,
            booking_id=booking_id,
            booking_number=booking_number,
            description=description or f"Points earned from booking {booking_number or booking_id}",
            created_by=created_by,
        )
        self._session.add(txn)
        await self._session.flush()

        logger.info(
            "loyalty_points_credited",
            customer_id=str(customer_id),
            points=points,
            new_balance=account.balance_points,
            booking_id=str(booking_id) if booking_id else None,
            booking_number=booking_number,
        )
        return account

    async def debit_points(
        self,
        customer_id: uuid.UUID,
        points: int,
        *,
        booking_id: uuid.UUID | None = None,
        booking_number: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
    ) -> LoyaltyAccount:
        """
        Deduct ``points`` from the customer's balance (redemption).

        Raises:
            ValueError: When balance is insufficient or account doesn't exist.
        """
        from app.loyalty.domain.rules import assert_sufficient_balance

        account = await self.get_account_by_customer(customer_id)
        if account is None:
            raise ValueError(f"No loyalty account found for customer {customer_id}.")

        assert_sufficient_balance(account.balance_points, points)

        account.balance_points -= points
        account.total_redeemed += points
        account.updated_at = datetime.utcnow()

        txn = LoyaltyTransaction(
            account_id=account.id,
            customer_id=customer_id,
            transaction_type=TransactionType.REDEEM.value,
            points=points,
            booking_id=booking_id,
            booking_number=booking_number,
            description=description or f"Points redeemed for booking {booking_number or booking_id}",
            created_by=created_by,
        )
        self._session.add(txn)
        await self._session.flush()

        logger.info(
            "loyalty_points_redeemed",
            customer_id=str(customer_id),
            points=points,
            new_balance=account.balance_points,
            booking_id=str(booking_id) if booking_id else None,
            booking_number=booking_number,
        )
        return account

    async def cancel_points(
        self,
        customer_id: uuid.UUID,
        points: int,
        *,
        booking_id: uuid.UUID | None = None,
        booking_number: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
    ) -> LoyaltyAccount:
        """
        Reverse earned points when a booking is cancelled.

        Deducts ``points`` from balance (down to a floor of 0 — never negative).
        Records a ``cancel`` transaction for auditability.

        Args:
            customer_id:    Customer UUID.
            points:         Points originally earned (to be reversed).
            booking_id:     Cancelled booking UUID.
            booking_number: Human-readable booking reference.
            description:    Optional human-readable note.
            created_by:     Source identifier.

        Returns:
            Updated LoyaltyAccount ORM instance.
        """
        account = await self.get_account_by_customer(customer_id)
        if account is None:
            logger.warning(
                "loyalty_cancel_no_account",
                customer_id=str(customer_id),
                booking_id=str(booking_id) if booking_id else None,
            )
            return None  # type: ignore[return-value]

        # Clamp: never let balance go below 0
        deduct = min(points, account.balance_points)

        if deduct > 0:
            account.balance_points -= deduct
            account.updated_at = datetime.utcnow()

        txn = LoyaltyTransaction(
            account_id=account.id,
            customer_id=customer_id,
            transaction_type=TransactionType.CANCEL.value,
            points=points,  # record original value even if we could only deduct partial
            booking_id=booking_id,
            booking_number=booking_number,
            description=description or f"Points reversed for cancelled booking {booking_number or booking_id}",
            created_by=created_by,
        )
        self._session.add(txn)
        await self._session.flush()

        logger.info(
            "loyalty_points_cancelled",
            customer_id=str(customer_id),
            points_reversed=deduct,
            original_points=points,
            new_balance=account.balance_points,
            booking_id=str(booking_id) if booking_id else None,
            booking_number=booking_number,
        )
        return account

    async def expire_account_if_needed(
        self,
        account: LoyaltyAccount,
        created_by: str = "system",
    ) -> bool:
        """
        Zero the account balance if ``points_expire_at`` has passed.

        Args:
            account:    LoyaltyAccount ORM instance to check.
            created_by: Source identifier for the expiry transaction.

        Returns:
            True when the account was expired, False otherwise.
        """
        from app.loyalty.domain.rules import is_account_expired

        if not is_account_expired(account.points_expire_at):
            return False

        if account.balance_points > 0:
            expired_points = account.balance_points
            account.balance_points = 0
            account.updated_at = datetime.utcnow()

            txn = LoyaltyTransaction(
                account_id=account.id,
                customer_id=account.customer_id,
                transaction_type=TransactionType.EXPIRE.value,
                points=expired_points,
                description="Points expired due to inactivity.",
                created_by=created_by,
            )
            self._session.add(txn)
            await self._session.flush()

            logger.info(
                "loyalty_points_expired",
                customer_id=str(account.customer_id),
                expired_points=expired_points,
            )

        return True

    async def adjust_points(
        self,
        customer_id: uuid.UUID,
        delta: int,
        *,
        description: str | None = None,
        created_by: str | None = None,
    ) -> LoyaltyAccount:
        """
        Manual admin adjustment (positive = add, negative = subtract).

        Balance is clamped to ≥ 0 on deduction.
        """
        account, _ = await self.get_or_create_account(customer_id)

        if delta > 0:
            account.balance_points += delta
            account.total_earned += delta
            txn_type = TransactionType.ADJUST.value
        else:
            deduct = min(abs(delta), account.balance_points)
            account.balance_points -= deduct
            delta = -deduct  # record actual deduction
            txn_type = TransactionType.ADJUST.value

        account.updated_at = datetime.utcnow()

        txn = LoyaltyTransaction(
            account_id=account.id,
            customer_id=customer_id,
            transaction_type=txn_type,
            points=abs(delta),
            description=description or "Manual adjustment.",
            created_by=created_by,
        )
        self._session.add(txn)
        await self._session.flush()
        return account

    # ── Transaction queries ───────────────────────────────────────────────────

    async def list_transactions(
        self,
        customer_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[LoyaltyTransaction]:
        """Return paginated transaction history for a customer (newest first)."""
        result = await self._session.execute(
            select(LoyaltyTransaction)
            .where(LoyaltyTransaction.customer_id == customer_id)
            .order_by(LoyaltyTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_transactions(self, customer_id: uuid.UUID) -> int:
        """Return total transaction count for a customer."""
        from sqlalchemy import func

        result = await self._session.execute(
            select(func.count(LoyaltyTransaction.id)).where(
                LoyaltyTransaction.customer_id == customer_id
            )
        )
        return result.scalar_one()
