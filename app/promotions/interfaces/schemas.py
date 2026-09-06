"""
app/promotions/interfaces/schemas.py
──────────────────────────────────────
Pydantic request/response schemas for the Promotions API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Tracker schemas ───────────────────────────────────────────────────────────

class LoyaltyTrackerResponse(BaseModel):
    """Loyalty progress for one (service, arrangement) pair."""

    id: uuid.UUID
    customer_id: uuid.UUID
    service_id: uuid.UUID
    service_arrangement_id: uuid.UUID | None
    service_name: str = ""
    booking_count: int
    bookings_required: int
    bookings_remaining: int
    progress_percentage: float
    total_bookings: int
    total_rewards_earned: int
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Reward schemas ────────────────────────────────────────────────────────────

class LoyaltyRewardResponse(BaseModel):
    """A single loyalty reward."""

    id: uuid.UUID
    customer_id: uuid.UUID
    service_id: uuid.UUID
    service_arrangement_id: uuid.UUID | None
    service_name: str = ""
    status: str
    earned_from_booking_id: uuid.UUID | None
    redeemed_in_booking_id: uuid.UUID | None
    redeemed_at: datetime | None
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class RedeemRewardRequest(BaseModel):
    """Request body for redeeming a loyalty reward."""

    booking_id: uuid.UUID | None = Field(
        default=None,
        description="Optional: the booking ID in which this free reward is being used.",
    )


# ── Composite responses ───────────────────────────────────────────────────────

class LoyaltyStatusResponse(BaseModel):
    """Full loyalty status for a customer: all trackers + available rewards."""

    trackers: list[LoyaltyTrackerResponse]
    available_rewards: list[LoyaltyRewardResponse]
    total_available_rewards: int


class PaginatedRewardsResponse(BaseModel):
    """Paginated list of rewards (used for admin endpoint)."""

    items: list[LoyaltyRewardResponse]
    total: int
    limit: int
    offset: int


# ── Tracker admin schemas ─────────────────────────────────────────────────────

class CreateLoyaltyTrackerRequest(BaseModel):
    """Request body for admin creation of a loyalty tracker."""

    customer_id: uuid.UUID = Field(description="UUID of the customer in ushauth.")
    service_id: uuid.UUID = Field(description="UUID of the service in ushauth.")
    service_arrangement_id: uuid.UUID | None = Field(
        default=None,
        description="Optional UUID of the service arrangement.",
    )
    service_name: str = Field(
        default="",
        description="Human-readable service name (optional, stored for display).",
    )
    bookings_required: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Number of confirmed branch bookings required to earn one reward.",
    )
    booking_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Initial booking count for the current cycle. "
            "Pass 1 when creating from a booking.confirmed event so the first booking is counted."
        ),
    )
    total_bookings: int = Field(
        default=0,
        ge=0,
        description="Initial lifetime total bookings. Defaults to booking_count if not supplied.",
    )


class UpdateLoyaltyTrackerRequest(BaseModel):
    """Request body for admin update of a loyalty tracker. All fields optional."""

    booking_count: int | None = Field(
        default=None,
        ge=0,
        description="Override the current booking count (current cycle).",
    )
    bookings_required: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Update the reward threshold.",
    )


class PaginatedTrackersResponse(BaseModel):
    """Paginated list of loyalty trackers (used for admin endpoint)."""

    items: list[LoyaltyTrackerResponse]
    total: int
    limit: int
    offset: int


# ── Internal endpoint schemas ─────────────────────────────────────────────────

class RecordLoyaltyBookingRequest(BaseModel):
    """
    Request body for the internal ``POST /internal/loyalty/record/`` endpoint.

    Called by ushnotice (via UshBookNPayClient) when a booking.confirmed event
    is received, carrying optional customer context for SQS event enrichment.
    """

    customer_id: uuid.UUID = Field(description="UUID of the customer.")
    service_id: uuid.UUID = Field(description="UUID of the service.")
    service_arrangement_id: uuid.UUID | None = Field(
        default=None,
        description="Optional UUID of the service arrangement.",
    )
    booking_id: uuid.UUID = Field(description="UUID of the confirmed booking.")
    booking_type: str = Field(
        default="branch",
        description="Booking type — only 'branch' bookings count toward loyalty.",
    )

    # Optional customer context — used to enrich the loyalty.rewarded SQS event
    customer_name: str = Field(default="", description="Customer's display name.")
    customer_email: str = Field(default="", description="Customer's email address.")
    customer_phone: str = Field(default="", description="Customer's phone number.")
    service_name: str = Field(default="", description="Service name.")
    is_eligible_for_loyalty: bool = Field(
        default=False,
        description="Whether the service is eligible for loyalty tracking. "
                    "If False, the request is silently ignored and no tracker is created.",
    )


class RecordLoyaltyBookingResponse(BaseModel):
    """Response from the internal loyalty record endpoint."""

    tracker: LoyaltyTrackerResponse | None = None
    reward: LoyaltyRewardResponse | None = None
    reward_issued: bool = False
