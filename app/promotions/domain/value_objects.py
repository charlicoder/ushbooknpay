"""
app/promotions/domain/value_objects.py
───────────────────────────────────────
Promotions domain enumerations.
"""

from __future__ import annotations

from enum import Enum


class LoyaltyRewardStatus(str, Enum):
    """Lifecycle states for a loyalty reward."""

    AVAILABLE = "available"
    REDEEMED = "redeemed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
