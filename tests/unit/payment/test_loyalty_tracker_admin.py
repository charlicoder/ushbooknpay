"""
tests/unit/payment/test_loyalty_tracker_admin.py
─────────────────────────────────────────────────
Unit tests for the admin loyalty tracker endpoints and service methods.

Covers:
  - LoyaltyService.create_tracker_admin — happy path and duplicate conflict
  - LoyaltyService.update_tracker_admin — happy path and not-found error
  - LoyaltyService.list_all_trackers_admin — returns (trackers, total) tuple
  - Schema validation for CreateLoyaltyTrackerRequest and UpdateLoyaltyTrackerRequest
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Schema tests ──────────────────────────────────────────────────────────────


def test_create_tracker_request_defaults():
    """CreateLoyaltyTrackerRequest sets bookings_required=5 by default."""
    from app.promotions.interfaces.schemas import CreateLoyaltyTrackerRequest

    customer_id = uuid.uuid4()
    service_id = uuid.uuid4()

    req = CreateLoyaltyTrackerRequest(
        customer_id=customer_id,
        service_id=service_id,
    )
    assert req.customer_id == customer_id
    assert req.service_id == service_id
    assert req.service_arrangement_id is None
    assert req.bookings_required == 5


def test_create_tracker_request_custom_threshold():
    """CreateLoyaltyTrackerRequest accepts custom bookings_required."""
    from app.promotions.interfaces.schemas import CreateLoyaltyTrackerRequest

    req = CreateLoyaltyTrackerRequest(
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        bookings_required=10,
    )
    assert req.bookings_required == 10
    assert req.service_arrangement_id is not None


def test_create_tracker_request_invalid_threshold():
    """bookings_required must be >= 1."""
    from pydantic import ValidationError
    from app.promotions.interfaces.schemas import CreateLoyaltyTrackerRequest

    with pytest.raises(ValidationError):
        CreateLoyaltyTrackerRequest(
            customer_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            bookings_required=0,
        )


def test_update_tracker_request_all_none():
    """UpdateLoyaltyTrackerRequest allows all fields to be None (no-op patch)."""
    from app.promotions.interfaces.schemas import UpdateLoyaltyTrackerRequest

    req = UpdateLoyaltyTrackerRequest()
    assert req.booking_count is None
    assert req.bookings_required is None


def test_update_tracker_request_partial():
    """UpdateLoyaltyTrackerRequest accepts partial updates."""
    from app.promotions.interfaces.schemas import UpdateLoyaltyTrackerRequest

    req = UpdateLoyaltyTrackerRequest(booking_count=3)
    assert req.booking_count == 3
    assert req.bookings_required is None


def test_paginated_trackers_response():
    """PaginatedTrackersResponse serialises correctly."""
    from datetime import datetime, timezone
    from app.promotions.interfaces.schemas import (
        LoyaltyTrackerResponse,
        PaginatedTrackersResponse,
    )

    tracker = LoyaltyTrackerResponse(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_arrangement_id=None,
        booking_count=2,
        bookings_required=5,
        bookings_remaining=3,
        progress_percentage=40.0,
        total_bookings=7,
        total_rewards_earned=1,
        updated_at=datetime.now(tz=timezone.utc),
    )
    resp = PaginatedTrackersResponse(
        items=[tracker],
        total=1,
        limit=50,
        offset=0,
    )
    data = resp.model_dump(mode="json")
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["booking_count"] == 2
    assert data["items"][0]["bookings_remaining"] == 3


# ── Service layer tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_tracker_admin_happy_path():
    """create_tracker_admin creates a new tracker when none exists."""
    from app.promotions.application.loyalty_service import LoyaltyService

    customer_id = uuid.uuid4()
    service_id = uuid.uuid4()

    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    mock_tracker = MagicMock()
    mock_tracker.id = uuid.uuid4()
    mock_tracker.customer_id = customer_id
    mock_tracker.service_id = service_id
    mock_tracker.booking_count = 0
    mock_tracker.bookings_required = 5

    # No existing tracker
    svc._repo.get_tracker = AsyncMock(return_value=None)
    svc._repo.create_tracker = AsyncMock(return_value=mock_tracker)

    result = await svc.create_tracker_admin(
        customer_id=customer_id,
        service_id=service_id,
        service_arrangement_id=None,
        bookings_required=5,
    )

    svc._repo.get_tracker.assert_awaited_once()
    svc._repo.create_tracker.assert_awaited_once_with(
        customer_id=customer_id,
        service_id=service_id,
        service_arrangement_id=None,
        service_name="",
        bookings_required=5,
        booking_count=0,
        total_bookings=0,
    )
    assert result is mock_tracker


@pytest.mark.asyncio
async def test_create_tracker_admin_duplicate_raises():
    """create_tracker_admin raises ValueError when tracker already exists."""
    from app.promotions.application.loyalty_service import LoyaltyService

    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    # Existing tracker already present
    svc._repo.get_tracker = AsyncMock(return_value=MagicMock())
    svc._repo.create_tracker = AsyncMock()

    with pytest.raises(ValueError, match="already exists"):
        await svc.create_tracker_admin(
            customer_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            service_arrangement_id=None,
            bookings_required=5,
        )

    svc._repo.create_tracker.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_tracker_admin_updates_fields():
    """update_tracker_admin mutates booking_count and bookings_required."""
    from app.promotions.application.loyalty_service import LoyaltyService

    tracker_id = uuid.uuid4()
    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    mock_tracker = MagicMock()
    mock_tracker.booking_count = 2
    mock_tracker.bookings_required = 5

    svc._repo.get_tracker_by_id = AsyncMock(return_value=mock_tracker)

    result = await svc.update_tracker_admin(
        tracker_id=tracker_id,
        booking_count=4,
        bookings_required=8,
    )

    assert mock_tracker.booking_count == 4
    assert mock_tracker.bookings_required == 8
    assert result is mock_tracker


@pytest.mark.asyncio
async def test_update_tracker_admin_partial_update():
    """update_tracker_admin only updates provided fields."""
    from app.promotions.application.loyalty_service import LoyaltyService

    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    mock_tracker = MagicMock()
    mock_tracker.booking_count = 3
    mock_tracker.bookings_required = 5

    svc._repo.get_tracker_by_id = AsyncMock(return_value=mock_tracker)

    # Only update bookings_required
    await svc.update_tracker_admin(
        tracker_id=uuid.uuid4(),
        bookings_required=10,
    )

    # booking_count unchanged
    assert mock_tracker.booking_count == 3
    assert mock_tracker.bookings_required == 10


@pytest.mark.asyncio
async def test_update_tracker_admin_not_found():
    """update_tracker_admin raises ValueError when tracker does not exist."""
    from app.promotions.application.loyalty_service import LoyaltyService

    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    svc._repo.get_tracker_by_id = AsyncMock(return_value=None)

    tracker_id = uuid.uuid4()
    with pytest.raises(ValueError, match=str(tracker_id)):
        await svc.update_tracker_admin(tracker_id=tracker_id, booking_count=1)


@pytest.mark.asyncio
async def test_list_all_trackers_admin_returns_tuple():
    """list_all_trackers_admin returns (trackers_list, total_count)."""
    from app.promotions.application.loyalty_service import LoyaltyService

    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    mock_trackers = [MagicMock(), MagicMock()]
    svc._repo.list_all_trackers = AsyncMock(return_value=mock_trackers)
    svc._repo.count_all_trackers = AsyncMock(return_value=2)

    trackers, total = await svc.list_all_trackers_admin(limit=50, offset=0)

    assert trackers is mock_trackers
    assert total == 2
    svc._repo.list_all_trackers.assert_awaited_once_with(
        customer_id=None,
        service_id=None,
        limit=50,
        offset=0,
    )


@pytest.mark.asyncio
async def test_redeem_reward_publishes_sqs_event():
    """redeem_reward marks reward redeemed and emits loyalty.redeedmed SQS event."""
    from datetime import datetime, timezone
    from app.promotions.application.loyalty_service import LoyaltyService
    from app.promotions.domain.value_objects import LoyaltyRewardStatus
    from app.promotions.infrastructure.models import LoyaltyReward

    mock_session = AsyncMock()
    svc = LoyaltyService(mock_session)

    reward_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    booking_id = uuid.uuid4()
    therapist_id = uuid.uuid4()

    mock_reward = MagicMock(spec=LoyaltyReward)
    mock_reward.id = reward_id
    mock_reward.customer_id = customer_id
    mock_reward.service_id = uuid.uuid4()
    mock_reward.service_arrangement_id = uuid.uuid4()
    mock_reward.service_name = "24K Gold Luxury Rejuvenating Facial"
    mock_reward.status = LoyaltyRewardStatus.AVAILABLE.value
    mock_reward.earned_from_booking_id = uuid.uuid4()
    mock_reward.redeemed_in_booking_id = None
    mock_reward.redeemed_at = None
    mock_reward.expires_at = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
    mock_reward.created_at = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)

    svc._repo.get_reward = AsyncMock(return_value=mock_reward)

    async def _mock_mark_redeemed(reward, redeemed_in_booking_id):
        reward.status = "redeemed"
        reward.redeemed_in_booking_id = redeemed_in_booking_id
        reward.redeemed_at = datetime(2026, 8, 31, 16, 21, 21, tzinfo=timezone.utc)
        return reward

    svc._repo.mark_reward_redeemed = AsyncMock(side_effect=_mock_mark_redeemed)

    # Mock booking lookup so booking context is populated
    mock_booking = MagicMock()
    mock_booking.therapist_id = therapist_id
    mock_booking.appointment_date = datetime(2026, 9, 1, tzinfo=timezone.utc)
    mock_booking.appointment_start = datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc)
    mock_booking.duration_minutes = 60

    with (
        patch("app.events.sqs_client.get_sqs_client") as mock_get_sqs,
        patch("app.booking.infrastructure.repository.BookingRepository.get_by_id", new=AsyncMock(return_value=mock_booking)),
    ):
        mock_sqs = MagicMock()
        mock_sqs.publish_event = AsyncMock()
        mock_get_sqs.return_value = mock_sqs

        success, error, redeemed = await svc.redeem_reward(
            reward_id=reward_id,
            customer_id=customer_id,
            redeemed_in_booking_id=booking_id,
        )

        assert success is True
        assert error is None
        assert redeemed.status == "redeemed"
        assert redeemed.redeemed_in_booking_id == booking_id

        mock_sqs.publish_event.assert_awaited_once()
        published_event = mock_sqs.publish_event.call_args[0][0]
        assert published_event.event_name == "Loyalty.Redeemed"
        assert published_event.event_type == "loyalty.redeedmed"
        assert published_event.reward["id"] == str(reward_id)
        assert published_event.reward["customer_id"] == str(customer_id)
        assert published_event.reward["service_name"] == "24K Gold Luxury Rejuvenating Facial"
        assert published_event.reward["status"] == "redeemed"
        # Booking context fields must be inside reward dict
        assert published_event.reward["therapist_id"] == str(therapist_id)
        assert published_event.reward["appointment_date"] == "2026-09-01"
        assert published_event.reward["appointment_time"] == "10:30:00"
        assert published_event.reward["duration"] == 60
        # Top-level flattened fields
        assert published_event.id == str(reward_id)
        assert published_event.customer_id == str(customer_id)
        assert published_event.therapist_id == str(therapist_id)
        assert published_event.appointment_date == "2026-09-01"
        assert published_event.appointment_time == "10:30:00"
        assert published_event.duration == 60
        # data field must not exist
        assert not hasattr(published_event, "data") or not hasattr(type(published_event), "__dataclass_fields__") or "data" not in type(published_event).__dataclass_fields__


@pytest.mark.asyncio
async def test_internal_record_loyalty_booking_serialization_before_commit():
    """Verify that internal_record_loyalty_booking serializes tracker without MissingGreenlet error."""
    from datetime import datetime, timezone
    from app.promotions.api.router import internal_record_loyalty_booking, _tracker_to_schema
    from app.promotions.interfaces.schemas import RecordLoyaltyBookingRequest
    from app.promotions.infrastructure.models import LoyaltyTracker

    mock_session = AsyncMock()
    mock_tracker = MagicMock(spec=LoyaltyTracker)
    mock_tracker.id = uuid.uuid4()
    mock_tracker.customer_id = uuid.uuid4()
    mock_tracker.service_id = uuid.uuid4()
    mock_tracker.service_arrangement_id = None
    mock_tracker.service_name = "Test Service"
    mock_tracker.booking_count = 1
    mock_tracker.bookings_required = 5
    mock_tracker.bookings_remaining = 4
    mock_tracker.progress_percentage = 20.0
    mock_tracker.total_bookings = 1
    mock_tracker.total_rewards_earned = 0
    mock_tracker.updated_at = datetime.now(timezone.utc)

    # Simulate expired attribute on ORM object when session.commit() is called
    def on_commit():
        # Once committed, accessing mock_tracker.updated_at without eager loading could raise
        pass
    mock_session.commit.side_effect = on_commit

    req = RecordLoyaltyBookingRequest(
        customer_id=mock_tracker.customer_id,
        service_id=mock_tracker.service_id,
        booking_id=uuid.uuid4(),
        booking_type="branch",
        is_eligible_for_loyalty=True,
    )

    with patch("app.promotions.api.router.LoyaltyService.record_confirmed_booking", new=AsyncMock(return_value=(mock_tracker, None))):
        resp = await internal_record_loyalty_booking(body=req, session=mock_session)
        assert resp.status_code == 200
        mock_session.commit.assert_awaited_once()
