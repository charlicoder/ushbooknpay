"""
app/loyalty/interfaces/schemas.py
───────────────────────────────────
Pydantic v2 request / response schemas for the loyalty API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Response schemas ──────────────────────────────────────────────────────────


class LoyaltyAccountResponse(BaseModel):
    """Customer loyalty account summary."""

    id: str
    customer_id: str
    balance_points: int
    total_earned: int
    total_redeemed: int
    points_expire_at: datetime | None = None
    is_expired: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LoyaltyTransactionResponse(BaseModel):
    """Single ledger entry in the loyalty history."""

    id: str
    transaction_type: str
    points: int
    booking_id: str | None = None
    booking_number: str | None = None
    description: str | None = None
    created_at: datetime
    created_by: str | None = None

    model_config = {"from_attributes": True}


class LoyaltyTransactionListResponse(BaseModel):
    """Paginated list of loyalty transactions."""

    results: list[LoyaltyTransactionResponse]
    total: int
    limit: int
    offset: int


# ── Internal / request schemas ────────────────────────────────────────────────


class CreditPointsRequest(BaseModel):
    """
    POST /api/v1/loyalty/internal/credit/
    Called by ushnotice after processing a booking.confirmed event.
    """

    customer_id: uuid.UUID = Field(..., description="Customer UUID.")
    booking_id: uuid.UUID | None = Field(default=None, description="Confirmed booking UUID.")
    booking_number: str | None = Field(default=None, description="Human-readable booking reference.")
    loyalty_points: int = Field(default=0, ge=0, description="Service-level earn points.")
    arrangement_loyalty_points: int | None = Field(
        default=None,
        ge=0,
        description="Arrangement-level override (takes precedence when set and > 0).",
    )
    created_by: str = Field(default="ushnotice", description="Source identifier.")


class CancelPointsRequest(BaseModel):
    """
    POST /api/v1/loyalty/internal/cancel/
    Called by ushnotice when a booking.cancelled event with loyalty points is received.
    """

    customer_id: uuid.UUID
    booking_id: uuid.UUID | None = None
    booking_number: str | None = None
    points: int = Field(..., ge=0, description="Points originally earned for the cancelled booking.")
    created_by: str = Field(default="ushnotice")


class AdjustPointsRequest(BaseModel):
    """
    POST /api/v1/loyalty/admin/adjust/
    Manual admin adjustment.
    """

    customer_id: uuid.UUID
    delta: int = Field(..., description="Points to add (positive) or remove (negative).")
    description: str | None = Field(default=None, max_length=500)


class CreditPointsResponse(BaseModel):
    """Response after crediting points."""

    customer_id: str
    points_credited: int
    new_balance: int
    points_expire_at: datetime | None
    booking_id: str | None = None
    booking_number: str | None = None

class RedeemPointsRequest(BaseModel):
    """
    POST /api/v1/loyalty/redeem/
    Customer redemption request.
    """

    cost_in_points: int = Field(..., gt=0, description="Points to redeem.")
    booking_id: uuid.UUID | None = Field(default=None, description="Booking UUID if associated.")
    booking_number: str | None = Field(default=None, description="Booking reference.")
