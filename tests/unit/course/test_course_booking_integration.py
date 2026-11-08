"""Booking integration of course redemption (contract E): reserve / consume / release hooks and skip lists."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import Headers

from app.api.v1.bookings import create_booking
from app.booking.application.services import BookingService, _build_booking_event_data
from app.booking.domain.value_objects import BookingStatus, PricingBreakdown
from app.booking.infrastructure.models import Booking
from app.booking.interfaces.schemas import CreateBookingRequest
from app.course.domain.value_objects import CourseError
from app.events.contracts import BookingCancelledEvent, BookingCompletedEvent, BookingConfirmedEvent

from tests.unit.course.helpers import SERVICE_ID


def _course_booking(status: str = "confirmed", **overrides) -> Booking:
    b = MagicMock(spec=Booking)
    b.id = uuid.uuid4()
    b.booking_number = "BOK/2026/11/000001"
    b.customer_id = uuid.uuid4()
    b.customer_data = {"first_name": "Dana", "last_name": "Test", "phone_number": "+965", "email": "d@x"}
    b.branch_id = uuid.uuid4()
    b.branch_data = {"name": "Salmiya"}
    b.service_id = SERVICE_ID
    b.service_data = {"name": "Relaxing Massage", "is_eligible_for_loyalty": True, "loyalty_points": 10}
    b.services = []
    b.sessions = []
    b.service_arrangement_id = uuid.uuid4()
    b.service_arrangement_data = {"arrangement_name": "VIP 1", "arrangement_type": "vip_suite"}
    b.therapist_id = uuid.uuid4()
    b.therapist_data = {"name": "Farida"}
    b.secondary_therapist_id = None
    b.secondary_therapist_data = None
    b.appointment_start = datetime(2026, 12, 1, 10, 0, tzinfo=timezone.utc)
    b.appointment_end = datetime(2026, 12, 1, 11, 0, tzinfo=timezone.utc)
    b.duration_minutes = 60
    b.extra_minutes = 0
    b.total_duration = 60
    b.addons_duration = 0
    b.base_price = Decimal("20.000")
    for f in ("arrangement_price", "price_for_extra_minutes", "addon_price", "discount", "tax", "fees", "total_amount"):
        setattr(b, f, Decimal("0.000"))
    b.currency = "KWD"
    b.booking_type = "course_redemption"
    b.payment_type = "course_subscription"
    b.payment_method = "course_subscription"
    b.payment_provider = None
    b.status = status
    b.payment_status = "success"
    b.addons = []
    b.customer_notes = None
    b.internal_notes = None
    b.payment_data = {}
    b.created_at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    b.updated_at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    b.loyalty_data = None
    b.reward_id = None
    b.voucher_id = None
    b.voucher_data = None
    b.invoice_number = None
    b.course_subscription_id = uuid.uuid4()
    b.course_data = {"subscription_number": "CRS/2026/11/000001", "session_number": 2, "total_sessions": 5}
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def _service_with(booking) -> tuple[BookingService, MagicMock]:
    service = BookingService(session=AsyncMock())
    service._repo.get_by_id = AsyncMock(return_value=booking)
    service._repo.update = AsyncMock(return_value=booking)
    service._repo.delete_hold = AsyncMock()
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()
    service._cancel_booking_payment_links = AsyncMock()
    course = MagicMock()
    for name in ("on_booking_status", "release", "consume", "refresh_usage_snapshot", "validate_booking_edit",
                 "lock_for_booking", "reserve"):
        setattr(course, name, AsyncMock())
    service._course_service = MagicMock(return_value=course)  # type: ignore[method-assign]
    return service, course


# ── BookingService.create_booking ─────────────────────────────────────────────


def _pricing() -> PricingBreakdown:
    z = Decimal("0.000")
    return PricingBreakdown(arrangement_price=z, price_for_extra_minutes=z, addon_price=z, discount=z, tax=z, fees=z, total=z, currency="KWD")


def _create_kwargs(sub_id):
    return dict(
        customer_id=uuid.uuid4(),
        service_id=SERVICE_ID,
        therapist_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        appointment_start=datetime(2026, 12, 1, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 12, 1, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        pricing=_pricing(),
        status="confirmed",
        payment_status="success",
        booking_type="course_redemption",
        payment_type="course_subscription",
        course_subscription_id=sub_id,
        course_arrangement_type="vip_suite",
    )


@pytest.mark.asyncio
async def test_create_booking_locks_validates_and_reserves_in_same_transaction():
    service = BookingService(session=AsyncMock())
    order: list[str] = []
    sub = MagicMock()
    sub.id = uuid.uuid4()
    course = MagicMock()

    async def lock(*a, **kw):
        order.append("lock")
        return sub

    async def reserve(s, booking):
        order.append("reserve")
        booking.course_data = {"session_number": 1}

    course.lock_for_booking = AsyncMock(side_effect=lock)
    course.reserve = AsyncMock(side_effect=reserve)
    service._course_service = MagicMock(return_value=course)  # type: ignore[method-assign]
    service._repo.get_by_idempotency_key = AsyncMock(return_value=None)

    async def overlap(*a, **kw):
        order.append("overlap")
        return False

    async def create(b):
        order.append("create")
        return b

    service._repo.check_therapist_overlap = AsyncMock(side_effect=overlap)
    service._repo.create = AsyncMock(side_effect=create)
    service._repo.update = AsyncMock(side_effect=lambda b: b)
    service._repo.generate_booking_number = AsyncMock(return_value="BOK/2026/12/000001")
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()

    booking = await service.create_booking(**_create_kwargs(sub.id))

    assert order == ["lock", "overlap", "create", "reserve"]
    assert booking.course_subscription_id == sub.id
    assert booking.course_data == {"session_number": 1}
    lock_kwargs = course.lock_for_booking.call_args.kwargs
    assert lock_kwargs["arrangement_type"] == "vip_suite"
    events = [c.args[0] for c in service._enqueue_event.call_args_list]
    confirmed = next(e for e in events if isinstance(e, BookingConfirmedEvent))
    assert confirmed.is_course_redemption is True
    assert confirmed.is_eligible_for_loyalty is False
    assert confirmed.course_subscription_id == str(sub.id)


@pytest.mark.asyncio
async def test_create_booking_validation_error_creates_nothing():
    service = BookingService(session=AsyncMock())
    course = MagicMock()
    course.lock_for_booking = AsyncMock(side_effect=CourseError("COURSE_NO_SESSIONS_AVAILABLE"))
    service._course_service = MagicMock(return_value=course)  # type: ignore[method-assign]
    service._repo.get_by_idempotency_key = AsyncMock(return_value=None)
    service._repo.create = AsyncMock()
    with pytest.raises(CourseError):
        await service.create_booking(**_create_kwargs(uuid.uuid4()))
    service._repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_regular_booking_never_touches_course_service():
    service = BookingService(session=AsyncMock())
    service._course_service = MagicMock(side_effect=AssertionError("must not be called"))  # type: ignore[method-assign]
    service._repo.get_by_idempotency_key = AsyncMock(return_value=None)
    service._repo.check_therapist_overlap = AsyncMock(return_value=False)
    service._repo.create = AsyncMock(side_effect=lambda b: b)
    service._repo.create_hold = AsyncMock()
    service._record_status_change = AsyncMock()
    service._enqueue_event = AsyncMock()
    kwargs = _create_kwargs(None)
    kwargs.pop("course_subscription_id")
    kwargs.pop("course_arrangement_type")
    kwargs.update(booking_type="branch_service", payment_type="service")
    booking = await service.create_booking(**kwargs)
    assert booking.course_subscription_id is None


# ── status hooks ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("new_status", [BookingStatus.COMPLETED, BookingStatus.NO_SHOW, BookingStatus.CANCELLED])
@pytest.mark.asyncio
async def test_update_status_dispatches_course_hook(new_status):
    booking = _course_booking(status="confirmed")
    service, course = _service_with(booking)
    await service.update_status(booking.id, new_status, reason="desk")
    course.on_booking_status.assert_awaited_once()
    args, kwargs = course.on_booking_status.call_args
    assert args[1] == new_status.value
    course.refresh_usage_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_event_carries_course_fields():
    booking = _course_booking(status="confirmed")
    service, _course = _service_with(booking)
    await service.update_status(booking.id, BookingStatus.COMPLETED)
    ev = service._enqueue_event.call_args[0][0]
    assert isinstance(ev, BookingCompletedEvent)
    assert ev.is_course_redemption is True
    assert ev.course_subscription_number == "CRS/2026/11/000001"
    assert ev.course_session_number == 2
    assert ev.booking_type == "course_redemption" and ev.payment_type == "course_subscription"
    assert ev.loyalty_points == 0 and ev.is_eligible_for_loyalty is False


@pytest.mark.asyncio
async def test_non_course_status_change_skips_course_hook():
    booking = _course_booking(status="confirmed", booking_type="branch_service", payment_type="service")
    booking.course_subscription_id = None
    service, course = _service_with(booking)
    service._ensure_booking_loyalty_details = AsyncMock()
    await service.update_status(booking.id, BookingStatus.COMPLETED)
    service._course_service.assert_not_called()


@pytest.mark.asyncio
async def test_reschedule_of_course_booking_validates_expiry():
    booking = _course_booking(status="confirmed")
    service, course = _service_with(booking)
    service._repo.check_therapist_overlap = AsyncMock(return_value=False)
    await service.update_booking(booking.id, appointment_start=datetime(2026, 12, 2, 10, 0, tzinfo=timezone.utc))
    assert course.refresh_usage_snapshot.call_args.kwargs["validate_expiry"] is True


@pytest.mark.asyncio
async def test_adding_extras_to_course_booking_is_validated():
    booking = _course_booking(status="confirmed")
    service, course = _service_with(booking)
    course.validate_booking_edit = AsyncMock(side_effect=CourseError("COURSE_EXTRAS_NOT_ALLOWED"))
    with pytest.raises(CourseError):
        await service.update_booking(booking.id, addons=[{"addon_id": "a", "price": "5"}])


@pytest.mark.asyncio
async def test_cancel_course_booking_releases_and_requests_no_refund():
    booking = _course_booking(status="confirmed")
    service, course = _service_with(booking)
    service._ensure_cancellation_refund = AsyncMock()
    service._sync_booking_payment_status = AsyncMock()
    service._get_ushauth_client = MagicMock(return_value=None)

    await service.cancel_booking(booking.id, reason="sick", cancelled_by="ushdesk")

    course.release.assert_awaited_once()
    service._ensure_cancellation_refund.assert_not_called()
    service._sync_booking_payment_status.assert_not_called()
    ev = service._enqueue_event.call_args[0][0]
    assert isinstance(ev, BookingCancelledEvent)
    assert ev.refund_required is False and ev.refund_amount is None
    assert ev.is_course_redemption is True


# ── skip lists ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoice_loyalty_and_refund_skipped_for_course_booking():
    booking = _course_booking()
    service, _ = _service_with(booking)
    service._get_ushauth_client = MagicMock(side_effect=AssertionError("no loyalty refresh"))
    with patch("app.integrations.ushanr_client.UshanrClient", side_effect=AssertionError("no invoice")):
        assert await service._ensure_booking_invoice(booking) is None
    await service._ensure_booking_loyalty_details(booking)
    assert await service._ensure_cancellation_refund(booking) is None


def test_event_data_course_fields_and_no_loyalty():
    data = _build_booking_event_data(_course_booking())
    assert data["is_course_redemption"] is True
    assert data["course_subscription_number"] == "CRS/2026/11/000001"
    assert data["course_session_number"] == 2
    assert data["is_eligible_for_loyalty"] is False
    assert data["service_data"]["is_eligible_for_loyalty"] is False
    assert data["loyalty_points"] == 0


def test_event_data_regular_booking_course_fields_empty():
    b = _course_booking(booking_type="branch_service", payment_type="service")
    b.course_subscription_id = None
    b.course_data = None
    data = _build_booking_event_data(b)
    assert data["is_course_redemption"] is False
    assert data["course_subscription_id"] == "" and data["course_session_number"] is None
    assert data["is_eligible_for_loyalty"] is True


@pytest.mark.asyncio
async def test_loyalty_credit_skipped_for_course_booking():
    from app.loyalty.application.services import LoyaltyService

    booking = _course_booking()
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = booking
    session.execute = AsyncMock(return_value=result)
    svc = LoyaltyService(session)
    account = MagicMock()
    svc._repo = MagicMock()
    svc._repo.get_account_by_customer = AsyncMock(return_value=account)
    svc._repo.credit = AsyncMock(side_effect=AssertionError("must not credit"))
    out = await svc.credit_points(
        customer_id=booking.customer_id, loyalty_points=10, arrangement_loyalty_points=None, booking_id=booking.id
    )
    assert out is account


# ── API: POST /bookings with course_subscription_id ───────────────────────────


def _api_inputs(perms):
    created = _course_booking()
    mock_service = MagicMock(spec=BookingService)
    mock_service.create_booking = AsyncMock(return_value=created)
    mock_service._session = AsyncMock()
    tid = uuid.uuid4()
    ushauth = AsyncMock()
    ushauth.check_appointment_availability = AsyncMock(return_value={"available": "yes", "therapist_id": str(tid)})
    ushauth.get_customer_profile = AsyncMock(return_value={"data": {"user": {"first_name": "Dana"}}})
    ushauth.get_service_arrangement = AsyncMock(return_value={"data": {"arrangement_type": "vip_suite"}})
    ushauth.create_appointment_cache = AsyncMock()
    ushauth.get_therapist = AsyncMock(return_value={"data": {"name": "Farida"}})
    request = MagicMock(spec=Request)
    request.headers = Headers()
    user = MagicMock()
    user.sub = str(uuid.uuid4())
    user.user_type = "employee"
    user.has_permission = MagicMock(side_effect=lambda c: c in perms)
    body = CreateBookingRequest(
        customer_id=uuid.uuid4(),
        service_id=SERVICE_ID,
        branch_id=uuid.uuid4(),
        service_arrangement_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-12-01",
        appointment_time="10:00:00",
        base_price="20.000",
        total_price="20.000",
        base_duration=60,
        payment_method="knet",
        payment_provider="knet_pos",
        course_subscription_id=uuid.uuid4(),
    )
    return mock_service, ushauth, request, user, body


@pytest.mark.asyncio
async def test_api_create_course_booking_forces_server_fields():
    mock_service, ushauth, request, user, body = _api_inputs({"course_subscriptions.redeem"})
    with patch("app.integrations.ushanr_client.UshanrClient", side_effect=AssertionError("no invoice")):
        resp = await create_booking(
            request=request, body=body, current_user=user, booking_service=mock_service,
            ushauth=ushauth, settings=MagicMock(),
        )
    kw = mock_service.create_booking.call_args.kwargs
    assert kw["booking_type"] == "course_redemption"
    assert kw["payment_type"] == "course_subscription"
    assert kw["status"] == "confirmed" and kw["payment_status"] == "success"
    assert kw["pricing"].total == Decimal("0.000") and kw["pricing"].discount == Decimal("0.000")
    assert kw["payment_method"] == "course_subscription"
    assert kw["payment_provider"] == "Course Subscription"
    assert kw["payment_through"] == "ushdesk"
    assert kw["course_subscription_id"] == body.course_subscription_id
    assert kw["course_arrangement_type"] == "vip_suite"
    assert all(e["service_data"]["is_eligible_for_loyalty"] is False for e in kw["services"])
    # appointment cache booking_type is "branch"
    assert ushauth.create_appointment_cache.call_args.kwargs["booking_type"] == "branch"
    # availability was checked like a branch booking
    ushauth.check_appointment_availability.assert_awaited()
    assert resp.data.course_subscription_id == str(mock_service.create_booking.return_value.course_subscription_id)


@pytest.mark.asyncio
async def test_api_course_booking_requires_redeem_permission():
    mock_service, ushauth, request, user, body = _api_inputs(set())
    with pytest.raises(HTTPException) as exc:
        await create_booking(
            request=request, body=body, current_user=user, booking_service=mock_service,
            ushauth=ushauth, settings=MagicMock(),
        )
    assert exc.value.status_code == 403
    mock_service.create_booking.assert_not_called()


@pytest.mark.asyncio
async def test_api_course_error_keeps_contract_code():
    mock_service, ushauth, request, user, body = _api_inputs({"course_subscriptions.redeem"})
    mock_service.create_booking = AsyncMock(side_effect=CourseError("COURSE_SUBSCRIPTION_EXPIRED", "expired"))
    with pytest.raises(HTTPException) as exc:
        await create_booking(
            request=request, body=body, current_user=user, booking_service=mock_service,
            ushauth=ushauth, settings=MagicMock(),
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "COURSE_SUBSCRIPTION_EXPIRED"
