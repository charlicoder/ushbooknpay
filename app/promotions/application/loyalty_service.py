"""
app/promotions/application/loyalty_service.py
──────────────────────────────────────────────
Loyalty program business logic.

Entry points:
  record_confirmed_booking  — called when a BRANCH booking reaches CONFIRMED status.
                              Updates the tracker and issues a reward if threshold met.
                              Emits a ``loyalty.rewarded`` SQS event when a reward is issued.
  get_loyalty_status        — return tracker(s) for a customer
  get_customer_rewards      — return rewards for a customer
  redeem_reward             — mark a reward as used

Design:
  - Only branch bookings count (booking_type == "branch").
  - Tracks per (customer_id, service_id, service_arrangement_id).
  - Every 5 confirmed bookings → 1 LoyaltyReward created, counter resets to 0.
  - Reward expires 10 days after creation.
  - On reward issuance a ``loyalty.rewarded`` SQS event is published (best-effort).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.promotions.domain.value_objects import LoyaltyRewardStatus
from app.promotions.infrastructure.models import LoyaltyReward, LoyaltyTracker
from app.promotions.infrastructure.repository import LoyaltyRepository

logger = get_logger(__name__)


class LoyaltyService:
    """Orchestrates all loyalty reward operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = LoyaltyRepository(session)

    # ── Core: record a booking ────────────────────────────────────────────────

    async def record_confirmed_booking(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        service_arrangement_id: uuid.UUID | None,
        booking_id: uuid.UUID,
        booking_type: str,
        *,
        customer_name: str = "",
        customer_email: str = "",
        customer_phone: str = "",
        service_name: str = "",
    ) -> tuple[LoyaltyTracker, LoyaltyReward | None]:
        """
        Record a confirmed booking for loyalty tracking.

        Rules:
          - Only branch bookings count (booking_type == "branch").
          - Increments booking_count and total_bookings on the tracker.
          - Issues a LoyaltyReward when booking_count reaches bookings_required.
          - Resets booking_count to 0 after a reward is issued.
          - Emits a ``loyalty.rewarded`` SQS event when a reward is issued (best-effort).

        Args:
            customer_id: UUID of the customer.
            service_id: UUID of the service.
            service_arrangement_id: Optional UUID of the service arrangement.
            booking_id: UUID of the confirmed booking.
            booking_type: Must be "branch" — home service bookings are ineligible.
            customer_name: Customer's display name (for SQS event context).
            customer_email: Customer's email (for SQS event context).
            customer_phone: Customer's phone (for SQS event context).
            service_name: Service name (for SQS event context).

        Returns:
            (tracker, reward) — reward is None if threshold not yet reached.

        Raises:
            ValueError: If booking_type is not "branch".
        """
        if booking_type not in ("branch_service", "branch"):
            logger.info(
                "loyalty_skipped_non_branch",
                booking_id=str(booking_id),
                booking_type=booking_type,
            )
            raise ValueError(
                f"Loyalty tracking only applies to branch bookings, got: {booking_type!r}"
            )

        tracker, created = await self._repo.get_or_create_tracker(
            customer_id=customer_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
            service_name=service_name,
        )

        # ── Idempotency guard ─────────────────────────────────────────────────
        # SQS delivers messages at-least-once — the same booking.confirmed event
        # can arrive more than once. Skip silently if this booking_id was already
        # recorded so booking_count is never incremented more than once per booking.
        if tracker.last_recorded_booking_id == booking_id:
            logger.info(
                "loyalty_duplicate_booking_skipped",
                booking_id=str(booking_id),
                tracker_id=str(tracker.id),
            )
            reward_for_this_booking: LoyaltyReward | None = None
            # Return current state without modifying anything
            return tracker, reward_for_this_booking

        tracker.booking_count += 1
        tracker.total_bookings += 1
        tracker.last_recorded_booking_id = booking_id
        # Update service_name in case it was empty (old tracker before this column existed)
        if service_name and not tracker.service_name:
            tracker.service_name = service_name

        reward: LoyaltyReward | None = None

        if tracker.booking_count >= tracker.bookings_required:
            reward = await self._repo.create_reward(
                tracker=tracker,
                earned_from_booking_id=booking_id,
                service_name=service_name,
            )
            tracker.booking_count = 0
            tracker.total_rewards_earned += 1

            logger.info(
                "loyalty_reward_issued",
                customer_id=str(customer_id),
                service_id=str(service_id),
                service_arrangement_id=str(service_arrangement_id) if service_arrangement_id else None,
                booking_id=str(booking_id),
                reward_id=str(reward.id),
                total_rewards_earned=tracker.total_rewards_earned,
            )

            # Emit SQS event (best-effort — never blocks the caller)
            await self._publish_loyalty_rewarded_event(
                tracker=tracker,
                reward=reward,
                booking_id=booking_id,
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                service_name=service_name,
            )
        else:
            logger.info(
                "loyalty_progress_updated",
                customer_id=str(customer_id),
                service_id=str(service_id),
                booking_count=tracker.booking_count,
                bookings_required=tracker.bookings_required,
                bookings_remaining=tracker.bookings_remaining,
            )

        await self._session.flush()
        return tracker, reward

    async def _publish_loyalty_rewarded_event(
        self,
        tracker: LoyaltyTracker,
        reward: LoyaltyReward,
        booking_id: uuid.UUID,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        service_name: str,
    ) -> None:
        """
        Publish a ``loyalty.rewarded`` SQS event (best-effort, non-blocking).

        Any transport errors are caught and logged so that a connectivity issue
        never prevents the loyalty update from being persisted to the database.
        """
        try:
            from app.events.contracts import LoyaltyRewardedEvent
            from app.events.sqs_client import get_sqs_client

            expires_at_str = reward.expires_at.isoformat() if reward.expires_at else ""

            event = LoyaltyRewardedEvent(
                reward_id=str(reward.id),
                tracker_id=str(tracker.id),
                customer_id=str(tracker.customer_id),
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                service_id=str(tracker.service_id),
                service_name=service_name,
                service_arrangement_id=str(tracker.service_arrangement_id) if tracker.service_arrangement_id else "",
                bookings_required=tracker.bookings_required,
                total_rewards_earned=tracker.total_rewards_earned,
                reward_status=reward.status,
                expires_at=expires_at_str,
                booking_id=str(booking_id),
            )

            sqs = get_sqs_client()
            await sqs.publish_event(event)

            logger.info(
                "loyalty_rewarded_event_published",
                reward_id=str(reward.id),
                customer_id=str(tracker.customer_id),
            )
        except Exception as exc:
            # Never block the loyalty persistence on SQS failures
            logger.warning(
                "loyalty_rewarded_event_publish_failed",
                reward_id=str(reward.id),
                customer_id=str(tracker.customer_id),
                error=str(exc),
            )

    async def _publish_loyalty_redeemed_event(
        self,
        reward: LoyaltyReward,
    ) -> None:
        """
        Publish a ``loyalty.redeedmed`` SQS event (best-effort, non-blocking).

        Any transport errors are caught and logged so that a connectivity issue
        never prevents the loyalty update from being persisted to the database.
        """
        try:
            from app.events.contracts import LoyaltyRedeemedEvent
            from app.events.sqs_client import get_sqs_client
            from app.promotions.interfaces.schemas import LoyaltyRewardResponse

            reward_schema = LoyaltyRewardResponse(
                id=reward.id,
                customer_id=reward.customer_id,
                service_id=reward.service_id,
                service_arrangement_id=reward.service_arrangement_id,
                service_name=reward.service_name or "",
                status=reward.status,
                earned_from_booking_id=reward.earned_from_booking_id,
                redeemed_in_booking_id=reward.redeemed_in_booking_id,
                redeemed_at=reward.redeemed_at,
                expires_at=reward.expires_at,
                created_at=reward.created_at,
            )
            reward_dict = reward_schema.model_dump(mode="json")

            # ── Fetch linked booking to include appointment context ────────────
            therapist_id = ""
            appointment_date = ""
            appointment_time = ""
            duration = 0

            if reward.redeemed_in_booking_id:
                try:
                    from app.booking.infrastructure.repository import BookingRepository
                    booking_repo = BookingRepository(self._session)
                    booking = await booking_repo.get_by_id(reward.redeemed_in_booking_id)
                    therapist_id = str(booking.therapist_id) if booking.therapist_id else ""
                    appointment_date = (
                        booking.appointment_date.strftime("%Y-%m-%d")
                        if booking.appointment_date else ""
                    )
                    appointment_time = (
                        booking.appointment_start.strftime("%H:%M:%S")
                        if booking.appointment_start else ""
                    )
                    duration = booking.duration_minutes or 0
                except Exception as booking_exc:
                    logger.warning(
                        "loyalty_redeemed_booking_fetch_failed",
                        redeemed_in_booking_id=str(reward.redeemed_in_booking_id),
                        error=str(booking_exc),
                    )

            # Enrich the reward dict with booking context
            reward_dict["therapist_id"] = therapist_id
            reward_dict["appointment_date"] = appointment_date
            reward_dict["appointment_time"] = appointment_time
            reward_dict["duration"] = duration

            event = LoyaltyRedeemedEvent(
                reward=reward_dict,
                id=str(reward.id),
                reward_id=str(reward.id),
                customer_id=str(reward.customer_id),
                service_id=str(reward.service_id),
                service_arrangement_id=str(reward.service_arrangement_id) if reward.service_arrangement_id else None,
                service_name=reward.service_name or "",
                status=reward.status,
                earned_from_booking_id=str(reward.earned_from_booking_id) if reward.earned_from_booking_id else None,
                redeemed_in_booking_id=str(reward.redeemed_in_booking_id) if reward.redeemed_in_booking_id else None,
                redeemed_at=reward_dict.get("redeemed_at") or "",
                expires_at=reward_dict.get("expires_at") or "",
                created_at=reward_dict.get("created_at") or "",
                therapist_id=therapist_id,
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                duration=duration,
            )

            sqs = get_sqs_client()
            await sqs.publish_event(event)

            logger.info(
                "loyalty_redeemed_event_published",
                reward_id=str(reward.id),
                customer_id=str(reward.customer_id),
            )
        except Exception as exc:
            logger.warning(
                "loyalty_redeemed_event_publish_failed",
                reward_id=str(reward.id),
                customer_id=str(reward.customer_id),
                error=str(exc),
            )

    # ── Query: tracker status ─────────────────────────────────────────────────

    async def get_tracker(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        service_arrangement_id: uuid.UUID | None,
    ) -> LoyaltyTracker | None:
        """Return the loyalty tracker for a specific (customer, service, arrangement)."""
        return await self._repo.get_tracker(
            customer_id=customer_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
        )

    async def list_trackers(self, customer_id: uuid.UUID) -> Sequence[LoyaltyTracker]:
        """Return all loyalty trackers for a customer."""
        return await self._repo.list_trackers_for_customer(customer_id)

    async def get_tracker_by_id(
        self, tracker_id: uuid.UUID
    ) -> LoyaltyTracker | None:
        """Return a tracker by its ID."""
        return await self._repo.get_tracker_by_id(tracker_id)

    async def list_all_trackers_admin(
        self,
        customer_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[LoyaltyTracker], int]:
        """Admin: return (trackers, total_count) across all customers."""
        trackers = await self._repo.list_all_trackers(
            customer_id=customer_id,
            service_id=service_id,
            limit=limit,
            offset=offset,
        )
        total = await self._repo.count_all_trackers(
            customer_id=customer_id,
            service_id=service_id,
        )
        return trackers, total

    async def create_tracker_admin(
        self,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        service_arrangement_id: uuid.UUID | None,
        bookings_required: int,
        booking_count: int = 0,
        total_bookings: int = 0,
        service_name: str = "",
    ) -> LoyaltyTracker:
        """
        Admin: manually create a loyalty tracker.

        Pass booking_count=1 (and total_bookings=1) when creating from a
        booking.confirmed event so the first booking is properly counted.

        Raises:
            ValueError: if a tracker already exists for this (customer, service, arrangement).
        """
        existing = await self._repo.get_tracker(
            customer_id=customer_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
        )
        if existing:
            raise ValueError(
                "A loyalty tracker already exists for this customer/service/arrangement combination."
            )
        tracker = await self._repo.create_tracker(
            customer_id=customer_id,
            service_id=service_id,
            service_arrangement_id=service_arrangement_id,
            service_name=service_name,
            bookings_required=bookings_required,
            booking_count=booking_count,
            total_bookings=total_bookings,
        )
        return tracker

    async def update_tracker_admin(
        self,
        tracker_id: uuid.UUID,
        booking_count: int | None = None,
        bookings_required: int | None = None,
    ) -> LoyaltyTracker:
        """
        Admin: update a tracker's booking_count and/or bookings_required.

        Raises:
            ValueError: if tracker not found.
        """
        tracker = await self._repo.get_tracker_by_id(tracker_id)
        if tracker is None:
            raise ValueError(f"LoyaltyTracker {tracker_id} not found.")

        if booking_count is not None:
            tracker.booking_count = booking_count
        if bookings_required is not None:
            tracker.bookings_required = bookings_required

        await self._session.flush()
        return tracker

    # ── Query: rewards ────────────────────────────────────────────────────────

    async def list_customer_rewards(
        self,
        customer_id: uuid.UUID,
        status: str | None = None,
    ) -> Sequence[LoyaltyReward]:
        """Return rewards for a customer, optionally filtered by status."""
        return await self._repo.list_rewards_for_customer(
            customer_id=customer_id, status=status
        )

    async def list_all_rewards_admin(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[LoyaltyReward], int]:
        """Admin: return (rewards, total_count) across all customers."""
        rewards = await self._repo.list_all_rewards(status=status, limit=limit, offset=offset)
        total = await self._repo.count_all_rewards(status=status)
        return rewards, total

    # ── Redeem ────────────────────────────────────────────────────────────────

    async def redeem_reward(
        self,
        reward_id: uuid.UUID,
        customer_id: uuid.UUID,
        redeemed_in_booking_id: uuid.UUID | None = None,
    ) -> tuple[bool, str | None, LoyaltyReward | None]:
        """
        Redeem a loyalty reward.

        Returns:
            (success, error_message, reward)
        """
        reward = await self._repo.get_reward(reward_id)

        if reward is None:
            return False, "Reward not found.", None

        if reward.customer_id != customer_id:
            return False, "Access denied. This reward does not belong to you.", None

        if reward.status != LoyaltyRewardStatus.AVAILABLE.value:
            return False, f"Reward is not available (current status: {reward.status}).", reward

        now = datetime.now(tz=timezone.utc)
        if reward.expires_at and reward.expires_at < now:
            # Lazily expire it
            reward.status = LoyaltyRewardStatus.EXPIRED.value
            await self._session.flush()
            return False, "This reward has expired.", reward

        reward = await self._repo.mark_reward_redeemed(
            reward=reward,
            redeemed_in_booking_id=redeemed_in_booking_id,
        )

        logger.info(
            "loyalty_reward_redeemed",
            reward_id=str(reward_id),
            customer_id=str(customer_id),
            redeemed_in_booking_id=str(redeemed_in_booking_id) if redeemed_in_booking_id else None,
        )

        # Emit SQS event (best-effort — never blocks the caller)
        await self._publish_loyalty_redeemed_event(reward=reward)

        return True, None, reward
