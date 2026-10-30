"""Unit tests for PaymentWebhookService outcome handling and settlement."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.payment_links.application.settlement import SettlementOutcome
from app.payment_links.application.webhook_service import PaymentWebhookService
from app.payment_links.domain.gateway import (
    GatewayPaymentState,
    GatewayPaymentStatus,
    GatewayWebhookEvent,
    WebhookEventKind,
)
from app.payment_links.infrastructure.models import PaymentLink


@pytest.fixture
def mock_deps():
    session = MagicMock()
    session.execute = AsyncMock()
    settings = MagicMock()
    settings.PAYMENT_WEBHOOK_DEBUG_LOG = False
    settings.payment_webhook_debug = False
    settings.is_development = True
    settings.PAYMENT_WEBHOOK_MAX_AGE_SECONDS = 0
    gateway = MagicMock()
    gateway.name = "myfatoorah"
    gateway.provider_label = "myfatoorah"
    return session, settings, gateway


@pytest.mark.asyncio
async def test_handle_payment_event_with_string_outcome(mock_deps):
    """Ensure outcome as a plain string does not raise AttributeError: 'str' object has no attribute 'value'."""
    session, settings, gateway = mock_deps
    service = PaymentWebhookService(session, settings, gateway)

    link = MagicMock(spec=PaymentLink)
    link.id = uuid.uuid4()
    link.amount = Decimal("10.000")

    service._settlement.get_locked_link_by_any_identifier = AsyncMock(return_value=link)
    # Simulate settlement.apply returning a plain string "settled"
    service._settlement.apply = AsyncMock(return_value="settled")

    verified_state = GatewayPaymentState(
        status=GatewayPaymentStatus.PAID,
        payment_id="pay_123",
        invoice_id="inv_123",
        amount=Decimal("10.000"),
    )
    gateway.get_payment_state = AsyncMock(return_value=verified_state)

    event = GatewayWebhookEvent(
        kind=WebhookEventKind.PAYMENT_STATUS,
        invoice_id="inv_123",
        status=GatewayPaymentStatus.PAID,
        state=verified_state,
        event_reference="ref_123",
    )

    outcome, target_id = await service._handle_payment_event(event)
    assert outcome == "settled"
    assert target_id == link.id


@pytest.mark.asyncio
async def test_handle_payment_event_with_enum_outcome(mock_deps):
    """Ensure outcome as SettlementOutcome enum returns string value."""
    session, settings, gateway = mock_deps
    service = PaymentWebhookService(session, settings, gateway)

    link = MagicMock(spec=PaymentLink)
    link.id = uuid.uuid4()

    service._settlement.get_locked_link_by_any_identifier = AsyncMock(return_value=link)
    service._settlement.apply = AsyncMock(return_value=SettlementOutcome.SETTLED)

    verified_state = GatewayPaymentState(
        status=GatewayPaymentStatus.PAID,
        payment_id="pay_456",
        invoice_id="inv_456",
    )
    gateway.get_payment_state = AsyncMock(return_value=verified_state)

    event = GatewayWebhookEvent(
        kind=WebhookEventKind.PAYMENT_STATUS,
        invoice_id="inv_456",
        status=GatewayPaymentStatus.PAID,
        state=verified_state,
        event_reference="ref_456",
    )

    outcome, target_id = await service._handle_payment_event(event)
    assert outcome == "settled"
    assert target_id == link.id


@pytest.mark.asyncio
async def test_handle_payment_event_failed_outcome(mock_deps):
    """Ensure failed outcome also handles string or enum without error."""
    session, settings, gateway = mock_deps
    service = PaymentWebhookService(session, settings, gateway)

    link = MagicMock(spec=PaymentLink)
    link.id = uuid.uuid4()

    service._settlement.get_locked_link_by_any_identifier = AsyncMock(return_value=link)
    service._settlement.apply = AsyncMock(return_value="failure_recorded")

    failed_state = GatewayPaymentState(
        status=GatewayPaymentStatus.FAILED,
        payment_id="pay_fail",
        invoice_id="inv_fail",
        failure_reason="Card declined",
    )
    gateway.get_payment_state = AsyncMock(return_value=failed_state)

    event = GatewayWebhookEvent(
        kind=WebhookEventKind.PAYMENT_STATUS,
        invoice_id="inv_fail",
        status=GatewayPaymentStatus.FAILED,
        state=failed_state,
        event_reference="ref_fail",
    )

    outcome, target_id = await service._handle_payment_event(event)
    assert outcome == "failure_recorded"
    assert target_id == link.id


@pytest.mark.asyncio
async def test_process_webhook_end_to_end(mock_deps):
    session, settings, gateway = mock_deps
    service = PaymentWebhookService(session, settings, gateway)

    link = MagicMock(spec=PaymentLink)
    link.id = uuid.uuid4()

    gateway.verify_signature = MagicMock(return_value=True)
    verified_state = GatewayPaymentState(
        status=GatewayPaymentStatus.PAID,
        payment_id="pay_789",
        invoice_id="inv_789",
    )
    gateway.get_payment_state = AsyncMock(return_value=verified_state)

    event = GatewayWebhookEvent(
        kind=WebhookEventKind.PAYMENT_STATUS,
        invoice_id="inv_789",
        status=GatewayPaymentStatus.PAID,
        state=verified_state,
        event_reference="WH-789",
    )
    gateway.parse_event = MagicMock(return_value=event)

    service._claim_event = AsyncMock(return_value=uuid.uuid4())
    service._settlement.get_locked_link_by_any_identifier = AsyncMock(return_value=link)
    service._settlement.apply = AsyncMock(return_value=SettlementOutcome.SETTLED)

    headers = {"myfatoorah-signature": "test"}
    raw_body = json.dumps({"Data": {"InvoiceId": "inv_789"}}).encode()

    result = await service.process(headers, raw_body)
    assert result == {"received": True, "outcome": "settled"}
