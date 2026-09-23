"""
app/loyalty/infrastructure/models.py
──────────────────────────────────────
SQLAlchemy ORM models for the loyalty programme.

Tables:
  loyalty_account     — one row per customer (balance, expiry)
  loyalty_transaction — append-only ledger of every point movement
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class LoyaltyAccount(Base):
    """
    One loyalty account per customer.

    ``balance_points``  is the current redeemable balance.
    ``total_earned``    is a running tally — never decremented (useful for reporting).
    ``total_redeemed``  is a running tally of spent points.
    ``points_expire_at`` is reset to (now + LOYALTY_POINTS_EXPIRY_DAYS) on every
                         earn event; a lazy expiry check zeroes the balance when
                         this date is in the past.
    """

    __tablename__ = "loyalty_account"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        index=True,
        comment="FK to the Customer in ushauth (not enforced across services).",
    )
    balance_points: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Current redeemable balance.",
    )
    total_earned: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Cumulative points ever earned; never decremented.",
    )
    total_redeemed: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Cumulative points ever redeemed.",
    )
    points_expire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Rolling expiry; reset on every earn event.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )

    # Relationship
    transactions: Mapped[list[LoyaltyTransaction]] = relationship(
        "LoyaltyTransaction",
        back_populates="account",
        order_by="LoyaltyTransaction.created_at.desc()",
        lazy="raise",
    )

    __table_args__ = (
        Index("idx_loyalty_account_customer", "customer_id"),
        Index("idx_loyalty_account_expire", "points_expire_at"),
    )


class LoyaltyTransaction(Base):
    """
    Append-only ledger of every point movement.

    ``points`` is always a positive integer; the ``transaction_type`` field
    determines whether this is an addition (earn/adjust) or deduction
    (redeem/expire/cancel).

    ``booking_id`` and ``booking_number`` are both stored so that the
    transaction history is human-readable without joining to bookings.
    """

    __tablename__ = "loyalty_transaction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("loyalty_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised for fast per-customer queries without joining to loyalty_account
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    transaction_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="earn | redeem | expire | adjust | cancel",
    )
    points: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Absolute point value; always positive.",
    )
    # Booking references (nullable — adjustments and expirations have no booking)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    booking_number: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Human-readable booking reference for display in transaction history.",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        index=True,
    )
    created_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Source: 'ushnotice', 'admin', 'system', etc.",
    )

    # Relationship
    account: Mapped[LoyaltyAccount] = relationship(
        "LoyaltyAccount",
        back_populates="transactions",
    )

    __table_args__ = (
        Index("idx_loyalty_txn_account", "account_id"),
        Index("idx_loyalty_txn_customer", "customer_id"),
        Index("idx_loyalty_txn_booking", "booking_id"),
        Index("idx_loyalty_txn_created", "created_at"),
    )
