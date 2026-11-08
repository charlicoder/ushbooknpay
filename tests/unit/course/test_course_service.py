"""CourseSubscriptionService: purchase, activation, invoice, redemption lifecycle, expiry, refund."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import NotFoundError
from app.course.api.schemas import CreateCourseSubscriptionRequest, serialize_subscription
from app.course.domain.value_objects import CourseError
from app.events.contracts import (
    CourseSubscriptionActivatedEvent,
    CourseSubscriptionCreatedEvent,
    CourseSubscriptionExpiredEvent,
    CourseSubscriptionInvoiceCreatedEvent,
    CourseSubscriptionPaymentFailedEvent,
    CourseSubscriptionRefundedEvent,
    CourseSubscriptionSessionConsumedEvent,
    CourseSubscriptionSessionReleasedEvent,
    CourseSubscriptionSessionReservedEvent,
)
from app.payment.infrastructure.models import Payment, Refund

from tests.unit.course.helpers import (
    COURSE_ID,
    FakeUshanr,
    course_payload,
    make_booking,
    make_service,
    make_sub,
    now,
)

PMT = "app.payment.infrastructure.repository.generate_payment_number"
RFN = "app.payment.infrastructure.repository.generate_refund_number"


def _ushauth(course=None, profile=None):
    client = AsyncMock()
    client.get_course = AsyncMock(return_value=course or course_payload())
    client.get_customer_profile = AsyncMock(
        return_value=profile
        or {
            "data": {
                "first_name": "Dana",
                "last_name": "Test",
                "phone_number": "+96512345678",
                "email": "dana@example.com",
                "language_preference": "ar-KW",
                "whatsapp_verified": True,
            }
        }
    )
    return client


def _added(session, cls):
    return [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], cls)]


# ── purchase ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_uses_server_side_price_and_snapshot():
    ushauth = _ushauth(course_payload(price="0.000", unit_price="20.000", total_sessions=5))
    service, repo, _session, events = make_service(ushauth=ushauth)

    sub, created = await service.create_subscription(customer_id=uuid.uuid4(), course_id=COURSE_ID)

    assert created is True
    assert sub.price == Decimal("100.000")
    assert sub.status == "payment_pending"
    assert sub.subscription_number.startswith("CRS/")
    assert sub.arrangement_type == "vip_suite"
    assert sub.course_data["service_name"] == "Relaxing Massage"
    assert sub.customer_data["whatsapp_verified"] is True
    assert sub.customer_data["language_preference"] == "ar"
    assert sub.customer_data["name"] == "Dana Test"
    assert len(events) == 1 and isinstance(events[0], CourseSubscriptionCreatedEvent)
    ev = events[0]
    assert ev.event_type == "course_subscription.created"
    assert ev.event_name == "CourseSubscription.Created"
    assert ev.price == "100.000" and ev.total_sessions == 5 and ev.payment_option == "pay_at_desk"
    assert ev.customer_data["whatsapp_verified"] is True


def test_client_price_is_not_accepted_by_the_request_schema():
    body = CreateCourseSubscriptionRequest(customer_id=uuid.uuid4(), course_id=COURSE_ID, price="1.000")
    assert not hasattr(body, "price")


@pytest.mark.asyncio
async def test_create_rejects_inactive_course():
    service, *_ = make_service(ushauth=_ushauth(course_payload(status="inactive")))
    with pytest.raises(CourseError) as exc:
        await service.create_subscription(customer_id=uuid.uuid4(), course_id=COURSE_ID)
    assert exc.value.code == "COURSE_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_create_unknown_course():
    ushauth = _ushauth()
    ushauth.get_course = AsyncMock(side_effect=NotFoundError("nope"))
    service, *_ = make_service(ushauth=ushauth)
    with pytest.raises(CourseError) as exc:
        await service.create_subscription(customer_id=uuid.uuid4(), course_id=COURSE_ID)
    assert exc.value.code == "COURSE_NOT_FOUND"


@pytest.mark.asyncio
async def test_idempotency_key_returns_existing_subscription():
    ushauth = _ushauth()
    service, repo, _s, events = make_service(ushauth=ushauth)
    first, created1 = await service.create_subscription(
        customer_id=uuid.uuid4(), course_id=COURSE_ID, idempotency_key="sell-123"
    )
    second, created2 = await service.create_subscription(
        customer_id=uuid.uuid4(), course_id=COURSE_ID, idempotency_key="sell-123"
    )
    assert created1 is True and created2 is False
    assert first is second
    assert ushauth.get_course.await_count == 1
    assert len(repo.subs) == 1
    assert len(events) == 1


@pytest.mark.asyncio
async def test_create_with_payment_link_returns_payment_url():
    service, repo, _s, events = make_service(ushauth=_ushauth())

    async def fake_link(sub, gw, actor_id, actor_data, force_new=False):
        sub.payment_url = "https://pay.example/abc"
        sub.payment_link_id = uuid.uuid4()
        sub.payment_option = "payment_link"

    service._issue_payment_link = fake_link  # type: ignore[method-assign]
    sub, _ = await service.create_subscription(
        customer_id=uuid.uuid4(), course_id=COURSE_ID, payment_option="payment_link", payment_gateway="myfatoorah"
    )
    assert sub.payment_url == "https://pay.example/abc"
    assert events[0].payment_url == "https://pay.example/abc"
    assert events[0].payment_option == "payment_link"


# ── activation ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activate_creates_payment_invoice_and_events():
    sub = make_sub(status="payment_pending", activated_at=None, expires_at=None, invoice_status="pending")
    ushanr = FakeUshanr(invoice={"invoice_id": "inv-9", "invoice_number": "INV/2026/11/00009"})
    service, repo, session, events = make_service(sub, ushanr=ushanr)
    service._find_payment = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch(PMT, AsyncMock(return_value="PMT/2026/11/000001")):
        activated_sub, payment, activated = await service.activate(
            sub.id, payment_provider="knet_pos", transaction_reference="POS-1"
        )

    assert activated is True
    assert activated_sub.status == "active"
    assert sub.id in repo.locked  # row lock taken
    assert sub.expires_at == sub.activated_at + timedelta(days=90)
    assert isinstance(payment, Payment)
    assert payment.payment_for == "course_subscription"
    assert payment.course_subscription_id == sub.id
    assert payment.total_amount == Decimal("100.000")  # server-side price
    assert payment.invoice_number == "INV/2026/11/00009"
    assert sub.invoice_status == "created" and sub.invoice_number == "INV/2026/11/00009"
    assert sub.payment_number == "PMT/2026/11/000001"
    types = [type(e) for e in events]
    assert types == [CourseSubscriptionInvoiceCreatedEvent, CourseSubscriptionActivatedEvent]
    act = events[-1]
    assert act.payment_number == "PMT/2026/11/000001"
    assert act.invoice_number == "INV/2026/11/00009"
    assert act.activated_at and act.expires_at


@pytest.mark.asyncio
async def test_activation_is_idempotent_double_confirm_or_webhook():
    sub = make_sub(status="payment_pending", activated_at=None, expires_at=None, invoice_status="pending")
    service, repo, session, events = make_service(sub)
    service._find_payment = AsyncMock(return_value=None)  # type: ignore[method-assign]
    with patch(PMT, AsyncMock(return_value="PMT/2026/11/000001")):
        _, payment1, first = await service.activate(sub.id, payment_provider="knet_pos")
    service._find_payment = AsyncMock(return_value=payment1)  # type: ignore[method-assign]
    with patch(PMT, AsyncMock(return_value="PMT/2026/11/000002")) as gen:
        _, payment2, second = await service.activate(sub.id, payment_provider="myfatoorah")
        gen.assert_not_awaited()

    assert first is True and second is False
    assert payment2 is payment1
    assert len(_added(session, Payment)) == 1
    assert sum(isinstance(e, CourseSubscriptionActivatedEvent) for e in events) == 1


@pytest.mark.asyncio
async def test_activation_of_cancelled_subscription_is_rejected():
    sub = make_sub(status="cancelled")
    service, *_ = make_service(sub)
    service._find_payment = AsyncMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(CourseError) as exc:
        await service.activate(sub.id, payment_provider="knet_pos")
    assert exc.value.code == "COURSE_SUBSCRIPTION_NOT_PAYABLE"


@pytest.mark.asyncio
async def test_payment_failure_then_successful_retry():
    sub = make_sub(status="payment_pending", activated_at=None, expires_at=None, invoice_status="pending")
    service, repo, _s, events = make_service(sub)
    await service.mark_payment_failed(sub.id, reason="KNET declined")
    assert sub.status == "payment_failed"
    assert isinstance(events[-1], CourseSubscriptionPaymentFailedEvent)
    assert events[-1].reason == "KNET declined"
    # idempotent
    await service.mark_payment_failed(sub.id, reason="again")
    assert sum(isinstance(e, CourseSubscriptionPaymentFailedEvent) for e in events) == 1
    # not redeemable while failed
    assert serialize_subscription(sub)["is_redeemable"] is False

    service._find_payment = AsyncMock(return_value=None)  # type: ignore[method-assign]
    with patch(PMT, AsyncMock(return_value="PMT/2026/11/000003")):
        _, _, activated = await service.activate(sub.id, payment_provider="myfatoorah")
    assert activated is True and sub.status == "active"


@pytest.mark.asyncio
async def test_invoice_failure_marks_failed_and_retry_creates_it_once():
    sub = make_sub(status="payment_pending", activated_at=None, expires_at=None, invoice_status="pending")
    ushanr = FakeUshanr(invoice=None)  # ushanr down
    service, repo, _s, events = make_service(sub, ushanr=ushanr)
    service._find_payment = AsyncMock(return_value=None)  # type: ignore[method-assign]
    with patch(PMT, AsyncMock(return_value="PMT/2026/11/000004")):
        _, payment, activated = await service.activate(sub.id, payment_provider="knet_pos")

    assert activated is True and sub.status == "active"  # activation never blocked by accounting
    assert sub.invoice_status == "failed" and sub.invoice_number is None
    assert sub.invoice_last_error
    assert not any(isinstance(e, CourseSubscriptionInvoiceCreatedEvent) for e in events)

    ushanr.invoice = {"invoice_id": "inv-2", "invoice_number": "INV/2026/11/00002"}
    service._find_payment = AsyncMock(return_value=payment)  # type: ignore[method-assign]
    assert await service.retry_failed_invoices() == 1
    assert sub.invoice_status == "created" and payment.invoice_number == "INV/2026/11/00002"
    # nothing left to retry → no duplicate invoice call
    calls = ushanr.invoice_calls
    assert await service.retry_failed_invoices() == 0
    assert ushanr.invoice_calls == calls
    assert sum(isinstance(e, CourseSubscriptionInvoiceCreatedEvent) for e in events) == 1


# ── cancel / refund ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_only_before_payment():
    pending = make_sub(status="payment_pending")
    active = make_sub(status="active")
    service, *_ = make_service(pending, active)
    await service.cancel(pending.id, reason="changed mind")
    assert pending.status == "cancelled" and pending.cancel_reason == "changed mind"
    with pytest.raises(CourseError) as exc:
        await service.cancel(active.id)
    assert exc.value.code == "COURSE_CANCEL_NOT_ALLOWED"


@pytest.mark.parametrize("overrides", [{"used_sessions": 1}, {"reserved_sessions": 1}, {"status": "exhausted", "used_sessions": 5}])
@pytest.mark.asyncio
async def test_refund_guard_rejects(overrides):
    sub = make_sub(**overrides)
    service, *_ = make_service(sub)
    with pytest.raises(CourseError) as exc:
        await service.refund(sub.id, reason="x")
    assert exc.value.code == "COURSE_REFUND_NOT_ALLOWED"
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status", ["active", "expired"])
@pytest.mark.asyncio
async def test_refund_full_when_nothing_used(status):
    sub = make_sub(status=status, invoice_number="INV/1")
    payment = Payment(id=uuid.uuid4(), payment_number="PMT/1", status="success", total_amount=Decimal("100.000"))
    ushanr = FakeUshanr(credit_note={"credit_note_number": "CN/2026/11/00001", "credit_note_id": "cn-1"})
    service, _repo, session, events = make_service(sub, ushanr=ushanr)
    service._find_payment = AsyncMock(return_value=payment)  # type: ignore[method-assign]
    with patch(RFN, AsyncMock(return_value="REF/2026/11/000001")):
        await service.refund(sub.id, reason="customer request", refund_method="cash")

    assert sub.status == "refunded" and sub.refunded_at is not None
    assert payment.status == "refunded" and payment.amount_refunded == Decimal("100.000")
    refunds = _added(session, Refund)
    assert len(refunds) == 1 and refunds[0].refunded_amount == Decimal("100.000")
    assert ushanr.credit_note_kwargs["source_document_type"] == "course_subscription"
    ev = events[-1]
    assert isinstance(ev, CourseSubscriptionRefundedEvent)
    assert ev.credit_note_number == "CN/2026/11/00001" and ev.price == "100.000"
    # idempotent
    await service.refund(sub.id, reason="again")
    assert ushanr.credit_note_calls == 1


# ── redemption lifecycle ──────────────────────────────────────────────────────


async def _reserved(sub, service):
    booking = make_booking(sub)
    await service.reserve(sub, booking)
    return booking


@pytest.mark.asyncio
async def test_reserve_creates_usage_and_course_data():
    sub = make_sub()
    service, repo, _s, events = make_service(sub)
    booking = await _reserved(sub, service)

    assert sub.reserved_sessions == 1
    usage = next(iter(repo.usages.values()))
    assert usage.status == "reserved" and usage.session_number == 1
    assert usage.branch_name == "Salmiya" and usage.therapist_name == "Farida"
    assert booking.course_subscription_id == sub.id
    assert booking.course_data == {
        "subscription_id": str(sub.id),
        "subscription_number": sub.subscription_number,
        "course_id": str(sub.course_id),
        "course_code": "COURSE-001",
        "course_name": "Relaxation Package",
        "session_number": 1,
        "total_sessions": 5,
    }
    ev = events[-1]
    assert isinstance(ev, CourseSubscriptionSessionReservedEvent)
    assert ev.available_sessions == 4 and ev.booking_id == str(booking.id) and ev.session_number == 1


@pytest.mark.asyncio
async def test_cancel_releases_the_session():
    sub = make_sub()
    service, repo, _s, events = make_service(sub)
    booking = await _reserved(sub, service)
    await service.on_booking_status(booking, "cancelled", reason="customer cancelled")
    usage = next(iter(repo.usages.values()))
    assert usage.status == "released" and usage.release_reason == "customer cancelled"
    assert sub.reserved_sessions == 0 and sub.used_sessions == 0
    assert isinstance(events[-1], CourseSubscriptionSessionReleasedEvent)
    assert events[-1].available_sessions == 5
    # duplicate cancel = no-op
    await service.on_booking_status(booking, "cancelled")
    assert sub.reserved_sessions == 0


@pytest.mark.asyncio
async def test_complete_consumes_once_even_if_repeated():
    sub = make_sub()
    service, repo, _s, events = make_service(sub)
    booking = await _reserved(sub, service)
    await service.on_booking_status(booking, "completed")
    await service.on_booking_status(booking, "completed")  # duplicate event / update
    assert sub.used_sessions == 1 and sub.reserved_sessions == 0
    consumed = [e for e in events if isinstance(e, CourseSubscriptionSessionConsumedEvent)]
    assert len(consumed) == 1
    assert consumed[0].reason == "completed" and consumed[0].used_sessions == 1 and consumed[0].total_sessions == 5
    # consumed is terminal: cancelling afterwards does not give it back
    await service.on_booking_status(booking, "cancelled")
    assert sub.used_sessions == 1 and sub.reserved_sessions == 0


@pytest.mark.asyncio
async def test_no_show_consumes_the_session():
    sub = make_sub()
    service, repo, _s, events = make_service(sub)
    booking = await _reserved(sub, service)
    await service.on_booking_status(booking, "no_show")
    assert sub.used_sessions == 1
    usage = next(iter(repo.usages.values()))
    assert usage.status == "consumed" and usage.consumed_reason == "no_show"


@pytest.mark.asyncio
async def test_last_session_consumed_exhausts_subscription():
    sub = make_sub(total_sessions=2, used_sessions=1)
    service, *_ = make_service(sub)
    booking = await _reserved(sub, service)
    await service.consume(booking, reason="completed")
    assert sub.status == "exhausted"
    assert serialize_subscription(sub)["is_redeemable"] is False


@pytest.mark.asyncio
async def test_reserved_session_can_complete_after_expiry():
    sub = make_sub()
    service, *_ = make_service(sub)
    booking = await _reserved(sub, service)
    sub.status = "expired"
    sub.expires_at = now() - timedelta(hours=1)
    await service.consume(booking, reason="completed")
    assert sub.used_sessions == 1 and sub.status == "expired"


@pytest.mark.asyncio
async def test_reschedule_after_expiry_rejected():
    sub = make_sub(expires_at=now() + timedelta(days=3))
    service, *_ = make_service(sub)
    booking = await _reserved(sub, service)
    booking.appointment_start = now() + timedelta(days=4)
    with pytest.raises(CourseError) as exc:
        await service.refresh_usage_snapshot(booking, validate_expiry=True)
    assert exc.value.code == "COURSE_APPOINTMENT_AFTER_EXPIRY"


@pytest.mark.asyncio
async def test_reschedule_refreshes_snapshot():
    sub = make_sub()
    service, repo, *_ = make_service(sub)
    booking = await _reserved(sub, service)
    new_start = now() + timedelta(days=5)
    booking.appointment_start = new_start
    booking.therapist_data = {"name": "Maryam"}
    await service.refresh_usage_snapshot(booking, validate_expiry=True)
    usage = next(iter(repo.usages.values()))
    assert usage.appointment_start == new_start and usage.therapist_name == "Maryam"


@pytest.mark.asyncio
async def test_lock_for_booking_takes_row_lock_and_validates():
    sub = make_sub(used_sessions=5)
    service, repo, *_ = make_service(sub)
    with pytest.raises(CourseError) as exc:
        await service.lock_for_booking(
            sub.id,
            customer_id=sub.customer_id,
            services=[{"service_id": str(sub.service_id)}],
            appointment_start=now() + timedelta(days=1),
            arrangement_type="vip_suite",
        )
    assert exc.value.code == "COURSE_NO_SESSIONS_AVAILABLE"
    assert repo.locked == [sub.id]


@pytest.mark.asyncio
async def test_lock_for_booking_unknown_subscription():
    service, *_ = make_service()
    with pytest.raises(CourseError) as exc:
        await service.lock_for_booking(
            uuid.uuid4(), customer_id=uuid.uuid4(), services=[], appointment_start=now()
        )
    assert exc.value.code == "COURSE_SUBSCRIPTION_NOT_FOUND"


# ── expiry task ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expire_due_marks_expired_and_reports_lost_sessions():
    due = make_sub(expires_at=now() - timedelta(minutes=5), used_sessions=1, reserved_sessions=1)
    fresh = make_sub(expires_at=now() + timedelta(days=1))
    exhausted = make_sub(status="exhausted", used_sessions=5, expires_at=now() - timedelta(days=1))
    service, _repo, _s, events = make_service(due, fresh, exhausted)

    assert await service.expire_due() == 1
    assert due.status == "expired" and fresh.status == "active" and exhausted.status == "exhausted"
    ev = events[-1]
    assert isinstance(ev, CourseSubscriptionExpiredEvent)
    assert ev.lost_sessions == 3 and ev.subscription_id == str(due.id)
    # the reserved session stays valid after expiry
    assert due.reserved_sessions == 1


def test_serializer_contract_fields():
    sub = make_sub(used_sessions=1, reserved_sessions=1)
    data = serialize_subscription(sub, sessions=[])
    for key in (
        "subscription_number", "customer_data", "course_data", "course_code", "course_name", "service_name",
        "arrangement_type", "total_sessions", "used_sessions", "reserved_sessions", "available_sessions",
        "price", "currency", "status", "effective_status", "is_expired", "is_redeemable", "purchased_at",
        "activated_at", "expires_at", "payment_id", "payment_number", "invoice_status", "refund_id", "sessions",
    ):
        assert key in data
    assert data["available_sessions"] == 3 and data["price"] == "100.000"

