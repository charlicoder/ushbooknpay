"""Transactional outbox: writer staging, after-commit hook, relay retry / backoff / dead-letter, event payloads."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.events.contracts import (
    CourseSubscriptionActivatedEvent,
    CourseSubscriptionExpiredEvent,
    CourseSubscriptionRefundedEvent,
    CourseSubscriptionSessionConsumedEvent,
)
from app.events.outbox import relay, writer
from app.events.outbox.models import OutboxEvent


def _row(retry_count: int = 0, max_retries: int = 5) -> OutboxEvent:
    return OutboxEvent(
        id=uuid.uuid4(),
        event_name="CourseSubscription.Activated",
        queue_url="https://sqs.example/q",
        payload={"event_id": "e1", "event_type": "course_subscription.activated", "event_name": "CourseSubscription.Activated"},
        status="pending",
        retry_count=retry_count,
        max_retries=max_retries,
        scheduled_at=datetime.now(timezone.utc),
    )


# ── writer ────────────────────────────────────────────────────────────────────


def test_add_outbox_event_stages_row_in_callers_session():
    session = MagicMock()
    ev = CourseSubscriptionActivatedEvent(subscription_id="s1", subscription_number="CRS/2026/11/000001")
    row = writer.add_outbox_event(session, ev, correlation_id="s1")
    session.add.assert_called_once_with(row)
    assert row.status == "pending" and row.retry_count == 0
    assert row.event_name == "CourseSubscription.Activated"
    assert row.payload["event_type"] == "course_subscription.activated"
    assert row.payload["source"] == "ushbooknpay"
    assert row.payload["event_id"] == ev.event_id
    json.dumps(row.payload)  # JSON safe


@pytest.mark.asyncio
async def test_after_commit_hook_publishes_and_rollback_discards():
    class FakeSync:
        def __init__(self):
            self.info: dict = {}

    class FakeSession:
        def __init__(self):
            self.sync_session = FakeSync()
            self.added = []

        def add(self, obj):
            self.added.append(obj)

    session = FakeSession()
    with patch.object(writer.sa_event, "listen") as listen, patch(
        "app.events.outbox.relay.publish_outbox_ids", AsyncMock()
    ) as publish:
        writer.add_outbox_event(session, CourseSubscriptionActivatedEvent(subscription_id="a"))
        writer.add_outbox_event(session, CourseSubscriptionActivatedEvent(subscription_id="b"))
        assert listen.call_count == 2  # after_commit + after_rollback registered once
        ids = list(session.sync_session.info[writer._PENDING_KEY])
        assert len(ids) == 2

        writer._after_commit(session.sync_session)
        for task in list(writer._background_tasks):
            await task
        publish.assert_awaited_once_with(ids)
        assert writer._PENDING_KEY not in session.sync_session.info

        writer.add_outbox_event(session, CourseSubscriptionActivatedEvent(subscription_id="c"))
        writer._after_rollback(session.sync_session)
        writer._after_commit(session.sync_session)
        assert publish.await_count == 1  # rolled back rows are never published


# ── relay ─────────────────────────────────────────────────────────────────────


def test_delivery_success_marks_published():
    row = _row()
    assert relay.apply_delivery_result(row, None) == "published"
    assert row.status == "published" and row.published_at is not None


def test_delivery_failure_backs_off_exponentially():
    row = _row()
    t0 = datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert relay.apply_delivery_result(row, RuntimeError("sqs down"), now=t0) == "retried"
    assert row.status == "pending" and row.retry_count == 1
    assert row.scheduled_at == t0 + timedelta(seconds=5)
    relay.apply_delivery_result(row, RuntimeError("sqs down"), now=t0)
    assert row.scheduled_at == t0 + timedelta(seconds=10)
    assert relay.backoff_seconds(30) == 15 * 60


def test_dead_letter_after_max_attempts():
    row = _row(retry_count=4, max_retries=5)
    assert relay.apply_delivery_result(row, RuntimeError("boom")) == "dead"
    assert row.status == "failed" and row.retry_count == 5 and "boom" in row.last_error


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeFactory:
    def __init__(self, rows):
        self.session = MagicMock()
        self.session.execute = AsyncMock(return_value=_Result(rows))
        self.session.commit = AsyncMock()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_process_outbox_once_retries_then_dead_letters():
    good, flaky, dying = _row(), _row(), _row(retry_count=4)
    sqs = MagicMock()

    async def send_message(**kw):
        json.loads(kw["message_body"])  # valid JSON body
        if kw["message_attributes"]["event_id"] == "flaky":
            raise RuntimeError("timeout")
        if kw["message_attributes"]["event_id"] == "dying":
            raise RuntimeError("permanent")
        return {"MessageId": "1"}

    flaky.payload = {**flaky.payload, "event_id": "flaky"}
    dying.payload = {**dying.payload, "event_id": "dying"}
    sqs.send_message = AsyncMock(side_effect=send_message)
    factory = _FakeFactory([good, flaky, dying])
    with patch("app.events.sqs_client.get_sqs_client", return_value=sqs):
        stats = await relay.process_outbox_once(session_factory=factory)

    assert stats == {"published": 1, "retried": 1, "dead": 1}
    assert good.status == "published"
    assert flaky.status == "pending" and flaky.retry_count == 1
    assert dying.status == "failed"
    factory.session.commit.assert_awaited_once()
    attrs = sqs.send_message.call_args_list[0].kwargs["message_attributes"]
    assert attrs["event_type"] == "course_subscription.activated"


# ── event payloads (contract F) ───────────────────────────────────────────────


def test_course_event_envelopes_and_flat_payloads():
    consumed = CourseSubscriptionSessionConsumedEvent(
        subscription_id="s", subscription_number="CRS/1", customer_id="c",
        customer_data={"whatsapp_verified": True, "language_preference": "ar"},
        course_name="Relax", booking_id="b", booking_number="BOK/1", session_number=2,
        appointment_start="2026-12-01T13:00:00+03:00", available_sessions=3,
        reason="completed", used_sessions=2, total_sessions=5,
    ).to_dict()
    assert consumed["event_type"] == "course_subscription.session_consumed"
    assert consumed["event_name"] == "CourseSubscription.SessionConsumed"
    for key in ("event_id", "occurred_at", "schema_version", "source"):
        assert key in consumed
    assert consumed["source"] == "ushbooknpay"
    assert consumed["used_sessions"] == 2 and consumed["reason"] == "completed"
    assert consumed["customer_data"]["whatsapp_verified"] is True

    expired = CourseSubscriptionExpiredEvent(lost_sessions=3).to_dict()
    assert expired["event_type"] == "course_subscription.expired" and expired["lost_sessions"] == 3
    refunded = CourseSubscriptionRefundedEvent(credit_note_number="CN/1").to_dict()
    assert refunded["event_type"] == "course_subscription.refunded"
    activated = CourseSubscriptionActivatedEvent().to_dict()
    for key in ("payment_number", "invoice_number", "activated_at", "expires_at", "payment_url", "price"):
        assert key in activated
