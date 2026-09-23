"""
app/loyalty/domain/models.py
─────────────────────────────
Pure Python domain models for the loyalty programme.
No SQLAlchemy, no FastAPI — just plain Python dataclasses and enums.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TransactionType(str, Enum):
    """Loyalty transaction types."""

    EARN = "earn"        # Points awarded after a confirmed booking
    REDEEM = "redeem"    # Points spent to pay for a service
    EXPIRE = "expire"    # Balance zeroed after 180 days inactivity
    ADJUST = "adjust"    # Manual admin adjustment (add or subtract)
    CANCEL = "cancel"    # Points reversed when a booking is cancelled


@dataclass
class LoyaltyAccountDomain:
    """
    In-memory representation of a customer loyalty account.

    Attributes:
        id:               Account UUID.
        customer_id:      Owner customer UUID.
        balance_points:   Current redeemable balance.
        total_earned:     Cumulative points ever earned (never decremented).
        total_redeemed:   Cumulative points ever spent.
        points_expire_at: Rolling expiry datetime (reset on every earn event).
        created_at:       Record creation timestamp.
        updated_at:       Last update timestamp.
    """

    id: uuid.UUID
    customer_id: uuid.UUID
    balance_points: int
    total_earned: int
    total_redeemed: int
    points_expire_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class LoyaltyTransactionDomain:
    """
    A single ledger entry in a customer's loyalty history.

    Attributes:
        id:               Transaction UUID.
        account_id:       Parent loyalty account UUID.
        customer_id:      Denormalised customer UUID for fast lookup.
        transaction_type: Earn / Redeem / Expire / Adjust / Cancel.
        points:           Absolute point value (always positive; type implies direction).
        booking_id:       Associated booking UUID (nullable).
        booking_number:   Human-readable booking reference (nullable).
        description:      Human-readable note.
        created_at:       Timestamp.
        created_by:       Source system or user who created the entry.
    """

    id: uuid.UUID
    account_id: uuid.UUID
    customer_id: uuid.UUID
    transaction_type: TransactionType
    points: int
    booking_id: uuid.UUID | None
    booking_number: str | None
    description: str | None
    created_at: datetime
    created_by: str | None
