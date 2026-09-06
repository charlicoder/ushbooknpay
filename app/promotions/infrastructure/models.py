"""
app/promotions/infrastructure/models.py
────────────────────────────────────────
SQLAlchemy ORM models for the Promotions / Loyalty domain.

Tables:
  promotions_loyalty_tracker  — rolling counter per (customer, service, arrangement)
  promotions_loyalty_reward   — individual free-booking rewards earned

Design notes:
  - customer_id / service_id / service_arrangement_id are plain UUID columns
    (no FK constraint) because those entities live in ushauth, a separate service.
  - earned_from_booking_id / redeemed_in_booking_id are real FKs to the local
    bookings table.
  - Table prefix `promotions_*` makes future microservice extraction straightforward.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.promotions.domain.value_objects import LoyaltyRewardStatus

# ── Constants ────────────────────────────────────────────────────────────────
LOYALTY_BOOKINGS_REQUIRED = 5
# LOYALTY_REWARD_EXPIRY_DAYS is now driven by settings (LOYALTY_REWARD_EXPIRY_DAYS env var, default 60)


def _default_reward_expiry() -> datetime:
    """Return the default expiry datetime for a loyalty reward.

    Reads LOYALTY_REWARD_EXPIRY_DAYS from settings (default: 60 days).
    Falls back to 60 days if settings cannot be loaded.
    """
    try:
        from app.core.config import get_settings
        days = get_settings().LOYALTY_REWARD_EXPIRY_DAYS
    except Exception:
        days = 60
    return datetime.now(tz=timezone.utc) + timedelta(days=days)


# ── LoyaltyTracker ───────────────────────────────────────────────────────────

class LoyaltyTracker(Base):
    """
    Tracks loyalty progress per customer per service per arrangement.

    Incremented on every CONFIRMED branch booking for an eligible service.
    When `booking_count` reaches `bookings_required` (default 5),
    a LoyaltyReward is issued and the counter resets to 0.

    Only branch bookings count — home bookings are excluded.
    """

    __tablename__ = "promotions_loyalty_tracker"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # References to ushauth entities — plain UUIDs, no FK constraint
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Human-readable service name snapshot (denormalised for display)
    service_name: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="", default=""
    )

    # Rolling counter — resets to 0 after reward is issued
    booking_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


    # Configurable per-tracker (defaults to global constant)
    bookings_required: Mapped[int] = mapped_column(
        Integer, nullable=False, default=LOYALTY_BOOKINGS_REQUIRED
    )

    # Lifetime statistics (never reset)
    total_bookings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_rewards_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Idempotency guard: tracks the last booking_id that was recorded.
    # If the same booking_id arrives twice (SQS at-least-once delivery), the
    # second call is skipped without double-incrementing booking_count.
    last_recorded_booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationship to rewards earned through this tracker
    rewards: Mapped[list["LoyaltyReward"]] = relationship(
        "LoyaltyReward",
        back_populates="tracker",
        lazy="select",
    )

    __table_args__ = (
        # Enforce uniqueness: one tracker per (customer, service, arrangement)
        UniqueConstraint(
            "customer_id",
            "service_id",
            "service_arrangement_id",
            name="uq_promotions_tracker_customer_service_arrangement",
        ),
        Index("ix_promotions_tracker_customer_id", "customer_id"),
        Index("ix_promotions_tracker_service_id", "service_id"),
        Index(
            "ix_promotions_tracker_lookup",
            "customer_id",
            "service_id",
            "service_arrangement_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<LoyaltyTracker customer={self.customer_id} "
            f"service={self.service_id} ({self.service_name!r}) "
            f"count={self.booking_count}/{self.bookings_required}>"
        )

    @property
    def progress_percentage(self) -> float:
        if self.bookings_required == 0:
            return 100.0
        return round((self.booking_count / self.bookings_required) * 100, 1)

    @property
    def bookings_remaining(self) -> int:
        return max(0, self.bookings_required - self.booking_count)


# ── LoyaltyReward ────────────────────────────────────────────────────────────

class LoyaltyReward(Base):
    """
    A one-time free booking reward earned through the loyalty program.

    Issued automatically when a customer completes the required number of
    CONFIRMED branch bookings for a service.
    """

    __tablename__ = "promotions_loyalty_reward"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # References to ushauth entities — plain UUIDs, no FK constraint
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Human-readable service name snapshot (denormalised for display)
    service_name: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="", default=""
    )

    # Back-reference to the tracker that generated this reward
    tracker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("promotions_loyalty_tracker.id", ondelete="SET NULL"),
        nullable=True,
    )
    tracker: Mapped["LoyaltyTracker | None"] = relationship(
        "LoyaltyTracker", back_populates="rewards"
    )


    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=LoyaltyRewardStatus.AVAILABLE.value,
    )

    # Booking that triggered the reward (5th booking)
    earned_from_booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Booking where the reward was used (the free booking)
    redeemed_in_booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
    )

    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Expires 10 days after creation by default
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_default_reward_expiry
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_promotions_reward_customer_status", "customer_id", "status"),
        Index("ix_promotions_reward_service_status", "service_id", "status"),
        Index("ix_promotions_reward_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<LoyaltyReward customer={self.customer_id} "
            f"service={self.service_id} ({self.service_name!r}) "
            f"status={self.status}>"
        )
