"""Payments integration: POST /payments (payment_for=course_subscription), payment-link settlement, API permissions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1.payments import create_payment
from app.core.database import get_db_session
from app.core.security import TokenPayload, require_authenticated_user
from app.course.api.router import router as course_router
from app.course.domain.value_objects import CourseError
from app.payment.infrastructure.models import Payment
from app.payment.interfaces.schemas import CreatePaymentRequestSchema
from app.payment_links.application.settlement import PaymentSettlementService, SettlementOutcome
from app.payment_links.domain.gateway import GatewayPaymentState, GatewayPaymentStatus

from tests.unit.course.helpers import make_sub

SVC = "app.course.application.services.CourseSubscriptionService"


def _payment(sub_id) -> Payment:
    return Payment(
        id=uuid.uuid4(),
        payment_number="PMT/2026/11/000001",
        customer_id=uuid.uuid4(),
        total_amount=Decimal("100.000"),
        total_duration=300,
        currency="KWD",
        status="success",
        payment_for="course_subscription",
        course_subscription_id=sub_id,
        created_at=datetime.now(timezone.utc),
    )


def _body(**kw) -> CreatePaymentRequestSchema:
    data = {"customer_id": uuid.uuid4(), "total_amount": "100.000", "total_duration": 0, "currency": "KWD",
            "payment_for": "course_subscription", "status": "success", "payment_provider": "knet_pos"}
    data.update(kw)
    return CreatePaymentRequestSchema(**data)


# ── POST /api/v1/payments/ ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_payment_course_requires_subscription_id():
    with pytest.raises(HTTPException) as exc:
        await create_payment(body=_body(), _=None, session=AsyncMock(), settings=MagicMock())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_create_payment_course_activates_and_dedupes():
    sub_id = uuid.uuid4()
    payment = _payment(sub_id)
    with patch(f"{SVC}.activate", AsyncMock(side_effect=[(MagicMock(), payment, True), (MagicMock(), payment, False)])) as act:
        first = await create_payment(body=_body(course_subscription_id=sub_id), _=None, session=AsyncMock(), settings=MagicMock())
        second = await create_payment(body=_body(course_subscription_id=sub_id), _=None, session=AsyncMock(), settings=MagicMock())
    assert first.status_code == 201 and second.status_code == 200
    assert act.await_args_list[0].args[0] == sub_id
    import json

    data = json.loads(first.body)["data"]
    assert data["payment_for"] == "course_subscription"
    assert data["course_subscription_id"] == str(sub_id)


@pytest.mark.asyncio
async def test_create_payment_course_failed_marks_payment_failed():
    sub = make_sub(status="payment_failed")
    with patch(f"{SVC}.mark_payment_failed", AsyncMock(return_value=sub)) as failed, patch(
        f"{SVC}.activate", AsyncMock(side_effect=AssertionError("no activation"))
    ):
        resp = await create_payment(
            body=_body(course_subscription_id=sub.id, status="failed"), _=None, session=AsyncMock(), settings=MagicMock()
        )
    assert resp.status_code == 200
    failed.assert_awaited_once()


# ── payment link settlement ───────────────────────────────────────────────────


def _link(target_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), target_type="course_subscription", target_id=target_id, status="pending",
        amount=Decimal("100.000"), currency="KWD", gateway="myfatoorah", provider_label="MyFatoorah",
        gateway_invoice_id="INV-1", gateway_payment_id="PAY-1", transaction_id="TX-1", track_id=None,
        reference_id=None, payment_method="knet_card", payment_url="https://pay/x", invoice_id=None,
        invoice_number=None, created_by="u1", created_by_data={}, meta={}, webhook_data=None,
        failure_data=None, paid_at=None, payment_row_id=None,
    )


def _paid_state() -> GatewayPaymentState:
    return GatewayPaymentState(status=GatewayPaymentStatus.PAID, invoice_id="INV-1", amount=Decimal("100.000"))


@pytest.mark.asyncio
async def test_settlement_activates_course_subscription():
    link = _link(uuid.uuid4())
    payment = _payment(link.target_id)
    session = AsyncMock()
    with patch(f"{SVC}.activate", AsyncMock(return_value=(MagicMock(), payment, True))) as act:
        outcome = await PaymentSettlementService(session, MagicMock()).apply(link, _paid_state(), source="webhook")
    assert outcome == SettlementOutcome.SETTLED
    assert link.status == "paid" and link.payment_row_id == payment.id
    kw = act.await_args.kwargs
    assert kw["payment_link"] is link and kw["payment_gateway"] == "myfatoorah"


@pytest.mark.asyncio
async def test_settlement_webhook_replay_is_noop():
    link = _link(uuid.uuid4())
    link.status = "paid"
    with patch(f"{SVC}.activate", AsyncMock(side_effect=AssertionError("must not run"))):
        outcome = await PaymentSettlementService(AsyncMock(), MagicMock()).apply(link, _paid_state(), source="webhook")
    assert outcome == SettlementOutcome.ALREADY_SETTLED


@pytest.mark.asyncio
async def test_settlement_cancelled_subscription_is_auto_refunded():
    link = _link(uuid.uuid4())
    gw = MagicMock()
    gw.refund_payment = AsyncMock()
    with patch(f"{SVC}.activate", AsyncMock(side_effect=CourseError("COURSE_SUBSCRIPTION_NOT_PAYABLE"))), patch(
        "app.payment_links.get_gateway", return_value=gw
    ), patch("app.api.deps.get_http_client", return_value=MagicMock()):
        outcome = await PaymentSettlementService(AsyncMock(), MagicMock()).apply(link, _paid_state(), source="webhook")
    assert outcome == SettlementOutcome.FAILURE_RECORDED
    assert link.status == "refunded"
    gw.refund_payment.assert_awaited_once()


@pytest.mark.asyncio
async def test_settlement_failure_marks_subscription_payment_failed():
    link = _link(uuid.uuid4())
    state = GatewayPaymentState(status=GatewayPaymentStatus.FAILED, invoice_id="INV-1", failure_reason="declined")
    with patch(f"{SVC}.mark_payment_failed", AsyncMock()) as failed:
        outcome = await PaymentSettlementService(AsyncMock(), MagicMock()).apply(link, state, source="webhook")
    assert outcome == SettlementOutcome.FAILURE_RECORDED
    failed.assert_awaited_once()
    assert failed.await_args.kwargs["reason"] == "declined"


# ── API permissions ───────────────────────────────────────────────────────────


def _client(user: TokenPayload) -> TestClient:
    app = FastAPI()
    app.include_router(course_router, prefix="/api/v1")

    async def _user():
        return user

    async def _db():
        yield AsyncMock()

    app.dependency_overrides[require_authenticated_user] = _user
    app.dependency_overrides[get_db_session] = _db
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("get", "/api/v1/course-subscriptions/", None),
        ("post", "/api/v1/course-subscriptions/", {"customer_id": str(uuid.uuid4()), "course_id": str(uuid.uuid4())}),
        ("post", f"/api/v1/course-subscriptions/{uuid.uuid4()}/refund/", {"reason": "x"}),
        ("post", f"/api/v1/course-subscriptions/{uuid.uuid4()}/confirm-payment/", {"payment_provider": "knet_pos"}),
    ],
)
def test_customers_and_unprivileged_staff_are_forbidden(method, path, body):
    for user in (
        TokenPayload(sub=str(uuid.uuid4()), user_type="customer"),
        TokenPayload(sub=str(uuid.uuid4()), user_type="employee", codenames=["bookings.list"]),
    ):
        resp = getattr(_client(user), method)(path, json=body) if body else getattr(_client(user), method)(path)
        assert resp.status_code == 403, (path, user.user_type)


def test_refund_requires_refund_codename_not_sell():
    user = TokenPayload(sub=str(uuid.uuid4()), user_type="employee", codenames=["course_subscriptions.sell"])
    resp = _client(user).post(f"/api/v1/course-subscriptions/{uuid.uuid4()}/refund/", json={"reason": "x"})
    assert resp.status_code == 403


def test_view_permission_lists_subscriptions():
    user = TokenPayload(sub=str(uuid.uuid4()), user_type="employee", codenames=["course_subscriptions.view"])
    sub = make_sub()
    with patch(
        "app.course.infrastructure.repository.CourseSubscriptionRepository.list_subscriptions",
        AsyncMock(return_value=([sub], 1)),
    ):
        resp = _client(user).get("/api/v1/course-subscriptions/?status=expired&page_size=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["meta"]["pagination"]["count"] == 1
    assert body["data"][0]["subscription_number"] == sub.subscription_number

