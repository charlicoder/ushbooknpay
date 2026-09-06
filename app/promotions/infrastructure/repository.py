"""
app/promotions/infrastructure/repository.py
────────────────────────────────────────────
Data access layer for the Promotions / Loyalty domain.

All methods accept an AsyncSession and return ORM instances.
The caller (application service) is responsible for commits.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.promotions.domain.value_objects import LoyaltyRewardStatus
from app.promotions.infrastructure.models import LoyaltyReward, LoyaltyTracker


class LoyaltyRepository:
    """All DB operations for loyalty trackers and rewards."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Tracker ──────────────────────────────────────────────────────────────

    async def get_tracker(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        service_arrangement_id: uuid.UUID | None,
    ) -> LoyaltyTracker | None:
        """Return the tracker for (customer, service, arrangement) or None."""
        stmt = select(LoyaltyTracker).where(
            LoyaltyTracker.customer_id == customer_id,
            LoyaltyTracker.service_id == service_id,
            LoyaltyTracker.service_arrangement_id == service_arrangement_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create_tracker(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        service_arrangement_id: uuid.UUID | None,
        service_name: str = "",
    ) -> tuple[LoyaltyTracker, bool]:
        """
        Return (tracker, created) — creates a new tracker if one does not exist.
        The caller must flush/commit the session after creation.
        """
        tracker = await self.get_tracker(customer_id, service_id, service_arrangement_id)
        if tracker is not None:
            return tracker, False

        tracker = LoyaltyTracker(
            customer_id=customer_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
            service_name=service_name,
        )
        self._session.add(tracker)
        await self._session.flush()  # Assign PK without committing
        return tracker, True

    async def list_trackers_for_customer(
        self, customer_id: uuid.UUID
    ) -> Sequence[LoyaltyTracker]:
        """Return all trackers for a customer, ordered by most recently updated."""
        stmt = (
            select(LoyaltyTracker)
            .where(LoyaltyTracker.customer_id == customer_id)
            .order_by(LoyaltyTracker.updated_at.desc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def get_tracker_by_id(
        self, tracker_id: uuid.UUID
    ) -> LoyaltyTracker | None:
        """Return a single tracker by its primary key."""
        stmt = select(LoyaltyTracker).where(LoyaltyTracker.id == tracker_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all_trackers(
        self,
        customer_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[LoyaltyTracker]:
        """Admin: return all trackers, optionally filtered by customer/service."""
        stmt = (
            select(LoyaltyTracker)
            .order_by(LoyaltyTracker.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if customer_id:
            stmt = stmt.where(LoyaltyTracker.customer_id == customer_id)
        if service_id:
            stmt = stmt.where(LoyaltyTracker.service_id == service_id)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_all_trackers(
        self,
        customer_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
    ) -> int:
        """Admin: total tracker count for pagination."""
        from sqlalchemy import func as sa_func
        stmt = select(sa_func.count()).select_from(LoyaltyTracker)
        if customer_id:
            stmt = stmt.where(LoyaltyTracker.customer_id == customer_id)
        if service_id:
            stmt = stmt.where(LoyaltyTracker.service_id == service_id)
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def create_tracker(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        service_arrangement_id: uuid.UUID | None,
        bookings_required: int,
        booking_count: int = 0,
        total_bookings: int = 0,
        service_name: str = "",
    ) -> LoyaltyTracker:
        """Admin: create a tracker directly with configurable initial counts."""
        tracker = LoyaltyTracker(
            customer_id=customer_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
            service_name=service_name,
            bookings_required=bookings_required,
            booking_count=booking_count,
            total_bookings=total_bookings if total_bookings > 0 else booking_count,
        )
        self._session.add(tracker)
        await self._session.flush()
        return tracker

    # ── Reward ───────────────────────────────────────────────────────────────

    async def create_reward(
        self,
        tracker: LoyaltyTracker,
        earned_from_booking_id: uuid.UUID | None = None,
        service_name: str = "",
    ) -> LoyaltyReward:
        """Create and flush a new LoyaltyReward for the given tracker."""
        reward = LoyaltyReward(
            customer_id=tracker.customer_id,
            service_id=tracker.service_id,
            service_arrangement_id=tracker.service_arrangement_id,
            service_name=service_name or tracker.service_name,
            tracker_id=tracker.id,
            earned_from_booking_id=earned_from_booking_id,
        )
        self._session.add(reward)
        await self._session.flush()
        return reward

    async def get_reward(self, reward_id: uuid.UUID) -> LoyaltyReward | None:
        """Return a single reward by ID."""
        stmt = select(LoyaltyReward).where(LoyaltyReward.id == reward_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_rewards_for_customer(
        self,
        customer_id: uuid.UUID,
        status: str | None = None,
    ) -> Sequence[LoyaltyReward]:
        """Return rewards for a customer, optionally filtered by status."""
        stmt = (
            select(LoyaltyReward)
            .where(LoyaltyReward.customer_id == customer_id)
            .order_by(LoyaltyReward.created_at.desc())
        )
        if status:
            stmt = stmt.where(LoyaltyReward.status == status)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_all_rewards(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[LoyaltyReward]:
        """Admin: return all rewards across all customers."""
        stmt = (
            select(LoyaltyReward)
            .order_by(LoyaltyReward.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status:
            stmt = stmt.where(LoyaltyReward.status == status)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_all_rewards(self, status: str | None = None) -> int:
        """Admin: total reward count for pagination."""
        from sqlalchemy import func as sa_func
        stmt = select(sa_func.count()).select_from(LoyaltyReward)
        if status:
            stmt = stmt.where(LoyaltyReward.status == status)
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def mark_reward_redeemed(
        self,
        reward: LoyaltyReward,
        redeemed_in_booking_id: uuid.UUID | None = None,
    ) -> LoyaltyReward:
        """Mark a reward as redeemed. Caller must commit."""
        reward.status = LoyaltyRewardStatus.REDEEMED.value
        reward.redeemed_at = datetime.now(tz=timezone.utc)
        reward.redeemed_in_booking_id = redeemed_in_booking_id
        await self._session.flush()
        return reward

    async def expire_stale_rewards(self) -> int:
        """
        Bulk-expire all AVAILABLE rewards past their expiry date.
        Returns the number of rows updated.
        """
        now = datetime.now(tz=timezone.utc)
        stmt = (
            update(LoyaltyReward)
            .where(
                LoyaltyReward.status == LoyaltyRewardStatus.AVAILABLE.value,
                LoyaltyReward.expires_at < now,
            )
            .values(status=LoyaltyRewardStatus.EXPIRED.value)
        )
        result = await self._session.execute(stmt)
        return result.rowcount
