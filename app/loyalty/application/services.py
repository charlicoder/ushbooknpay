"""
app/loyalty/application/services.py
─────────────────────────────────────
LoyaltyService — use-case orchestration for the loyalty programme.

This is the only entry-point that application code and API handlers
should use. It delegates persistence to LoyaltyRepository and
business validation to domain/rules.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.loyalty.domain.rules import compute_expiry, resolve_earn_points
from app.loyalty.infrastructure.models import LoyaltyAccount, LoyaltyTransaction
from app.loyalty.infrastructure.repository import LoyaltyRepository

logger = structlog.get_logger(__name__)


class LoyaltyService:
    """
    Application service for loyalty use cases.

    All methods operate within the passed ``session`` transaction.
    The caller (FastAPI dependency / test) owns commit/rollback.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = LoyaltyRepository(session)
        self._settings = get_settings()

    # ── Read use cases ─────────────────────────────────────────────────────────

    async def get_account(self, customer_id: uuid.UUID) -> LoyaltyAccount | None:
        """
        Return the customer's loyalty account with a lazy expiry check.

        If the account's ``points_expire_at`` is in the past the balance is
        zeroed and an expiry transaction is recorded before returning.

        Args:
            customer_id: Customer UUID.

        Returns:
            LoyaltyAccount ORM instance, or None if no account exists yet.
        """
        account = await self._repo.get_account_by_customer(customer_id)
        if account is None:
            return None

        # Lazy expiry check — zero balance if expired
        await self._repo.expire_account_if_needed(account)
        return account

    async def get_transactions(
        self,
        customer_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[LoyaltyTransaction], int]:
        """
        Return paginated loyalty transaction history for a customer.

        Args:
            customer_id: Customer UUID.
            limit:       Page size.
            offset:      Page offset.

        Returns:
            (transactions, total_count)
        """
        transactions = await self._repo.list_transactions(
            customer_id, limit=limit, offset=offset
        )
        total = await self._repo.count_transactions(customer_id)
        return transactions, total

    # ── Write use cases ────────────────────────────────────────────────────────

    async def credit_points(
        self,
        *,
        customer_id: uuid.UUID,
        loyalty_points: int,
        arrangement_loyalty_points: int | None,
        booking_id: uuid.UUID | None = None,
        booking_number: str | None = None,
        created_by: str = "ushnotice",
    ) -> LoyaltyAccount:
        """
        Award points to a customer after a confirmed booking.

        Points to credit are resolved by the domain rule:
          arrangement_loyalty_points (if set and > 0) > loyalty_points.

        The points expiry window is reset to now + LOYALTY_POINTS_EXPIRY_DAYS.

        Args:
            customer_id:                Customer UUID.
            loyalty_points:             Service-level earn points.
            arrangement_loyalty_points: Arrangement override (nullable).
            booking_id:                 Associated booking UUID.
            booking_number:             Human-readable booking reference.
            created_by:                 Source identifier.

        Returns:
            Updated LoyaltyAccount ORM instance.

        Raises:
            ValueError: When the resolved point value is 0.
        """
        # Check if booking is a loyalty redemption booking (redeemed points cannot earn points)
        if booking_id:
            try:
                from sqlalchemy import select
                from app.booking.infrastructure.models import Booking
                res = await self._session.execute(
                    select(Booking).where(Booking.id == booking_id)
                )
                bk = res.scalar_one_or_none()
                if bk is not None:
                    b_type = getattr(bk, "booking_type", "")
                    p_type = getattr(bk, "payment_type", "")
                    p_status = getattr(bk, "payment_status", "")
                    is_loyalty_bk = (
                        b_type == "loyalty"
                        or p_type == "rewarded"
                        or str(p_status).lower() == "rewarded"
                        or getattr(bk, "reward_id", None) is not None
                        or bool((getattr(bk, "loyalty_data", {}) or {}).get("points_cost"))
                        or bool((getattr(bk, "loyalty_data", {}) or {}).get("reward_id"))
                    )
                    if is_loyalty_bk:
                        logger.info(
                            "loyalty_credit_skipped_loyalty_redemption_booking",
                            customer_id=str(customer_id),
                            booking_id=str(booking_id),
                        )
                        account = await self._repo.get_account_by_customer(customer_id)
                        if account is None:
                            account, _ = await self._repo.get_or_create_account(customer_id)
                        return account
            except Exception as exc:
                logger.warning(
                    "loyalty_check_booking_failed",
                    booking_id=str(booking_id),
                    error=str(exc),
                )

        points = resolve_earn_points(loyalty_points, arrangement_loyalty_points)
        if points <= 0:
            logger.info(
                "loyalty_credit_skipped_zero_points",
                customer_id=str(customer_id),
                loyalty_points=loyalty_points,
                arrangement_loyalty_points=arrangement_loyalty_points,
            )
            # Return existing account without modifying it
            account = await self._repo.get_account_by_customer(customer_id)
            if account is None:
                account, _ = await self._repo.get_or_create_account(customer_id)
            return account

        expiry = compute_expiry(self._settings.LOYALTY_POINTS_EXPIRY_DAYS)
        return await self._repo.credit_points(
            customer_id=customer_id,
            points=points,
            expiry=expiry,
            booking_id=booking_id,
            booking_number=booking_number,
            created_by=created_by,
        )

    async def redeem_points(
        self,
        *,
        customer_id: uuid.UUID,
        cost_in_points: int,
        booking_id: uuid.UUID | None = None,
        booking_number: str | None = None,
        created_by: str = "api",
    ) -> LoyaltyAccount:
        """
        Deduct ``cost_in_points`` from a customer's balance (redemption).

        Performs a lazy expiry check first — expired accounts cannot redeem.

        Args:
            customer_id:    Customer UUID.
            cost_in_points: Points to spend (must be > 0).
            booking_id:     Associated booking UUID.
            booking_number: Human-readable booking reference.
            created_by:     Source identifier.

        Returns:
            Updated LoyaltyAccount ORM instance.

        Raises:
            ValueError: Insufficient balance or expired account.
        """
        account = await self.get_account(customer_id)
        if account is None:
            raise ValueError(f"No loyalty account found for customer {customer_id}.")

        return await self._repo.debit_points(
            customer_id=customer_id,
            points=cost_in_points,
            booking_id=booking_id,
            booking_number=booking_number,
            description=f"Points redeemed for booking {booking_number or booking_id}",
            created_by=created_by,
        )

    async def cancel_booking_points(
        self,
        *,
        customer_id: uuid.UUID,
        points: int,
        booking_id: uuid.UUID | None = None,
        booking_number: str | None = None,
        created_by: str = "ushnotice",
    ) -> LoyaltyAccount | None:
        """
        Reverse earned points when a booking is cancelled.

        Balance is clamped to ≥ 0 (never goes negative).

        Args:
            customer_id:    Customer UUID.
            points:         Points originally earned for the cancelled booking.
            booking_id:     Cancelled booking UUID.
            booking_number: Human-readable booking reference.
            created_by:     Source identifier.

        Returns:
            Updated LoyaltyAccount ORM instance, or None if no account exists.
        """
        if points <= 0:
            logger.info(
                "loyalty_cancel_skipped_zero_points",
                customer_id=str(customer_id),
                booking_id=str(booking_id) if booking_id else None,
            )
            return None

        return await self._repo.cancel_points(
            customer_id=customer_id,
            points=points,
            booking_id=booking_id,
            booking_number=booking_number,
            description=f"Points reversed for cancelled booking {booking_number or booking_id}",
            created_by=created_by,
        )

    async def adjust_points(
        self,
        *,
        customer_id: uuid.UUID,
        delta: int,
        description: str | None = None,
        created_by: str = "admin",
    ) -> LoyaltyAccount:
        """
        Manual admin adjustment (positive = add, negative = subtract).

        Args:
            customer_id: Customer UUID.
            delta:       Points to add (positive) or remove (negative).
            description: Human-readable reason.
            created_by:  Admin identifier.

        Returns:
            Updated LoyaltyAccount ORM instance.
        """
        return await self._repo.adjust_points(
            customer_id=customer_id,
            delta=delta,
            description=description,
            created_by=created_by,
        )
