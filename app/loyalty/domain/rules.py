"""
app/loyalty/domain/rules.py
────────────────────────────
Pure business rule functions for the loyalty programme.

All functions are stateless and side-effect-free — easy to unit test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── Constants ──────────────────────────────────────────────────────────────────

_DEFAULT_EXPIRY_DAYS = 180


# ── Business Rules ─────────────────────────────────────────────────────────────


def resolve_earn_points(
    loyalty_points: int,
    arrangement_loyalty_points: int | None,
) -> int:
    """
    Determine how many points to award for a confirmed booking.

    Resolution order:
      1. ``arrangement_loyalty_points`` if it is non-None and > 0
      2. ``loyalty_points`` (service-level default)

    Args:
        loyalty_points:             Points defined on the Service.
        arrangement_loyalty_points: Override points from ArrangementService (nullable).

    Returns:
        Points to credit (≥ 0).
    """
    if arrangement_loyalty_points is not None and arrangement_loyalty_points > 0:
        return arrangement_loyalty_points
    return max(0, loyalty_points)


def compute_expiry(expiry_days: int = _DEFAULT_EXPIRY_DAYS) -> datetime:
    """
    Compute a new rolling expiry timestamp from now.

    Args:
        expiry_days: Number of days from now until points expire.

    Returns:
        Timezone-aware UTC datetime.
    """
    return datetime.now(tz=timezone.utc) + timedelta(days=expiry_days)


def is_account_expired(points_expire_at: datetime | None) -> bool:
    """
    Return True when the account's points have passed their expiry date.

    A null ``points_expire_at`` means the account has never earned points —
    treated as not expired (balance is 0 anyway).

    Args:
        points_expire_at: Expiry timestamp stored on the loyalty account.

    Returns:
        True if points are expired, False otherwise.
    """
    if points_expire_at is None:
        return False
    return datetime.now(tz=timezone.utc) >= points_expire_at


def assert_sufficient_balance(balance_points: int, cost_in_points: int) -> None:
    """
    Raise ValueError if the account cannot cover the requested redemption cost.

    Args:
        balance_points:  Current account balance.
        cost_in_points:  Points required for the redemption.

    Raises:
        ValueError: With a human-readable message when balance is insufficient.
    """
    if cost_in_points <= 0:
        raise ValueError("Redemption cost must be greater than zero.")
    if balance_points < cost_in_points:
        raise ValueError(
            f"Insufficient loyalty points. "
            f"Balance: {balance_points}, required: {cost_in_points}."
        )
