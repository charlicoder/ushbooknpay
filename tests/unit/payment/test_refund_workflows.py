"""
tests/unit/payment/test_refund_workflows.py
───────────────────────────────────────────
Tests for booking refund workflows:
1. Sequential refund number generation (REF/YYYY/MM/{NNNNNN})
2. Manual refund recording (ushdesk: Cash, Card, Bank Transfer)
3. Automated payment gateway refund
4. Accounting immutability (original payment preserved, status history recorded)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.booking.infrastructure.models import Booking
from app.payment.application.refund_service import RefundService
from app.payment.domain.value_objects import (
    PaymentTransactionStatus,
    RefundMethod,
    RefundStatus,
    RefundType,
)
from app.payment.infrastructure.models import Payment, Refund
from app.payment.infrastructure.repository import generate_refund_number
from app.payment.interfaces.schemas import (
    CreateManualRefundRequest,
    ProcessGatewayRefundRequest,
)


@pytest.mark.asyncio
async def test_generate_refund_number_format():
    """Verify refund number adheres to REF/YYYY/MM/{NNNNNN} format."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 0
    mock_session.execute.return_value = mock_result

    test_date = date(2026, 10, 8)
    refund_number = await generate_refund_number(mock_session, for_date=test_date)

    assert refund_number == "REF/2026/10/000001"


@pytest.mark.asyncio
async def test_generate_refund_number_sequential_increment():
    """Verify refund sequence increments based on existing count."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 42
    mock_session.execute.return_value = mock_result

    test_date = date(2026, 10, 15)
    refund_number = await generate_refund_number(mock_session, for_date=test_date)

    assert refund_number == "REF/2026/10/000043"


@pytest.mark.asyncio
async def test_manual_refund_workflow():
    """
    Test recording a manual refund:
    - Customer receives cash/card at branch.
    - No payment-gateway transaction ID required.
    - Generates internal refund number.
    - Preserves payment record and updates amount_refunded.
    """
    booking_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    payment_id = uuid.uuid4()

    booking = Booking(
        id=booking_id,
        booking_number="BOK/2026/10/000001",
        customer_id=customer_id,
        customer_data={"name": "Fatima Al-Sabah", "phone": "+96599112233"},
        branch_id=branch_id,
        total_amount=Decimal("60.000"),
        currency="KWD",
        status="cancelled",
        payment_status="paid",
    )

    payment = Payment(
        id=payment_id,
        payment_number="PMT/2026/10/000001",
        booking_id=booking_id,
        customer_id=customer_id,
        total_amount=Decimal("60.000"),
        amount_refunded=Decimal("0.000"),
        currency="KWD",
        status=PaymentTransactionStatus.SUCCESS.value,
        payment_method="knet",
    )

    mock_session = AsyncMock()

    # Configure session.execute to return booking first, then payment, then count for refund number
    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "from bookings" in stmt_str:
            mock_res.scalar_one_or_none.return_value = booking
        elif "from payments" in stmt_str:
            mock_res.scalar_one_or_none.return_value = payment
        elif "count" in stmt_str:
            mock_res.scalar_one.return_value = 5
        return mock_res

    mock_session.execute.side_effect = mock_execute

    service = RefundService(session=mock_session)

    req = CreateManualRefundRequest(
        refund_amount=Decimal("60.000"),
        cancellation_fee=Decimal("0.000"),
        refund_method="cash",
        reason="Customer visited branch and requested cash refund",
        notes="Processed by reception desk",
        reference_number="REC-BRANCH-01",
    )

    with patch.object(service, "_publish_refund_event", new_callable=AsyncMock) as mock_pub:
        refund = await service.record_manual_refund(
            booking_id=booking_id,
            req=req,
            current_user_id="staff-uuid-123",
            current_user_data={"name": "Agent Sarah", "role": "desk"},
        )

        assert refund.refund_type == RefundType.MANUAL.value
        assert refund.refund_method == RefundMethod.CASH.value
        assert refund.status == RefundStatus.COMPLETED.value
        assert refund.refunded_amount == Decimal("60.000")
        assert refund.cancellation_fee == Decimal("0.000")
        assert refund.payment_gateway is None
        assert refund.gateway_refund_id is None
        assert refund.processed_by == "staff-uuid-123"
        assert refund.reference_number == "REC-BRANCH-01"

        # Verify payment immutability
        assert payment.total_amount == Decimal("60.000")  # Original unchanged!
        assert payment.amount_refunded == Decimal("60.000")
        assert payment.status == PaymentTransactionStatus.REFUNDED.value

        mock_pub.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_refund_with_cancellation_fee():
    """Test manual refund when a cancellation fee is deducted."""
    booking_id = uuid.uuid4()
    customer_id = uuid.uuid4()

    booking = Booking(
        id=booking_id,
        booking_number="BOK/2026/10/000002",
        customer_id=customer_id,
        total_amount=Decimal("60.000"),
        currency="KWD",
        status="cancelled",
        payment_status="paid",
    )

    payment = Payment(
        id=uuid.uuid4(),
        payment_number="PMT/2026/10/000002",
        booking_id=booking_id,
        customer_id=customer_id,
        total_amount=Decimal("60.000"),
        amount_refunded=Decimal("0.000"),
        currency="KWD",
        status=PaymentTransactionStatus.SUCCESS.value,
    )

    mock_session = AsyncMock()

    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "from bookings" in stmt_str:
            mock_res.scalar_one_or_none.return_value = booking
        elif "from payments" in stmt_str:
            mock_res.scalar_one_or_none.return_value = payment
        elif "count" in stmt_str:
            mock_res.scalar_one.return_value = 0
        return mock_res

    mock_session.execute.side_effect = mock_execute
    service = RefundService(session=mock_session)

    req = CreateManualRefundRequest(
        refund_amount=Decimal("60.000"),
        cancellation_fee=Decimal("10.000"),  # 10 KWD fee
        refund_method="bank_transfer",
        reason="Late cancellation policy applied",
    )

    with patch.object(service, "_publish_refund_event", new_callable=AsyncMock):
        refund = await service.record_manual_refund(
            booking_id=booking_id,
            req=req,
            current_user_id="staff-uuid-123",
        )

        assert refund.requested_amount == Decimal("60.000")
        assert refund.cancellation_fee == Decimal("10.000")
        assert refund.refunded_amount == Decimal("50.000")
        assert payment.amount_refunded == Decimal("50.000")
        assert payment.status == PaymentTransactionStatus.PARTIALLY_REFUNDED.value


@pytest.mark.asyncio
async def test_gateway_refund_workflow():
    """Test automated payment gateway refund via provider."""
    booking_id = uuid.uuid4()
    payment_id = uuid.uuid4()

    booking = Booking(
        id=booking_id,
        booking_number="BOK/2026/10/000003",
        customer_id=uuid.uuid4(),
        total_amount=Decimal("60.000"),
        currency="KWD",
        status="cancelled",
        payment_status="paid",
    )

    payment = Payment(
        id=payment_id,
        payment_number="PMT/2026/10/000003",
        booking_id=booking_id,
        customer_id=booking.customer_id,
        total_amount=Decimal("60.000"),
        amount_refunded=Decimal("0.000"),
        currency="KWD",
        status=PaymentTransactionStatus.SUCCESS.value,
        payment_provider="Tap",
        payment_gateway="TAP",
        payment_id="chg_123456",
        transaction_id="tx_123456",
    )

    mock_session = AsyncMock()

    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "from bookings" in stmt_str:
            mock_res.scalar_one_or_none.return_value = booking
        elif "from payments" in stmt_str:
            mock_res.scalar_one_or_none.return_value = payment
        elif "count" in stmt_str:
            mock_res.scalar_one.return_value = 10
        return mock_res

    mock_session.execute.side_effect = mock_execute
    service = RefundService(session=mock_session)

    mock_provider = MagicMock()
    mock_resp = MagicMock()
    mock_resp.is_successful = True
    mock_resp.provider_refund_id = "ref_tap_987654"
    mock_resp.raw_response = {"status": "SUCCESS"}
    mock_provider.refund_payment = AsyncMock(return_value=mock_resp)

    with patch.object(service, "_resolve_gateway_provider", return_value=mock_provider), \
         patch.object(service, "_publish_refund_event", new_callable=AsyncMock):

        req = ProcessGatewayRefundRequest(
            refund_amount=Decimal("60.000"),
            reason="Automated customer cancellation",
        )

        refund = await service.process_gateway_refund(
            booking_id=booking_id,
            req=req,
            current_user_id="user-456",
        )

        assert refund.refund_type == RefundType.AUTOMATED.value
        assert refund.refund_method == RefundMethod.PAYMENT_GATEWAY.value
        assert refund.status == RefundStatus.COMPLETED.value
        assert refund.gateway_refund_id == "ref_tap_987654"
        assert refund.payment_gateway == "TAP"
        assert payment.amount_refunded == Decimal("60.000")
        assert payment.status == PaymentTransactionStatus.REFUNDED.value


@pytest.mark.asyncio
async def test_refund_service_list_refunds_with_filters_and_pagination():
    mock_session = AsyncMock()

    refund_1 = MagicMock(spec=Refund)
    refund_1.id = uuid.uuid4()
    refund_1.refund_number = "REF/2026/10/000001"
    refund_1.refund_type = "manual"
    refund_1.refund_method = "cash"
    refund_1.status = "completed"
    refund_1.requested_amount = Decimal("50.000")
    refund_1.cancellation_fee = Decimal("5.000")
    refund_1.refunded_amount = Decimal("45.000")
    refund_1.currency = "KWD"
    refund_1.created_at = datetime.now(timezone.utc)

    # Mock count, aggregates, and data query
    mock_count_res = MagicMock()
    mock_count_res.scalar_one.return_value = 1

    mock_agg_row = MagicMock()
    mock_agg_row.total_refunded_amount = Decimal("45.000")
    mock_agg_row.total_cancellation_fee = Decimal("5.000")
    mock_agg_row.total_requested_amount = Decimal("50.000")
    mock_agg_res = MagicMock()
    mock_agg_res.one.return_value = mock_agg_row

    mock_data_res = MagicMock()
    mock_data_res.scalars.return_value.all.return_value = [refund_1]

    async def mock_exec(stmt):
        stmt_str = str(stmt).lower()
        if "count" in stmt_str:
            return mock_count_res
        elif "sum" in stmt_str:
            return mock_agg_res
        return mock_data_res

    mock_session.execute.side_effect = mock_exec

    service = RefundService(session=mock_session)
    refunds, total, analytics = await service.list_refunds(
        page=1,
        page_size=10,
        status="completed",
        refund_method="cash",
        search="REF/2026",
    )

    assert len(refunds) == 1
    assert total == 1
    assert analytics["total_refunded_amount"] == "45.000"
    assert analytics["total_cancellation_fee"] == "5.000"
    assert analytics["total_requested_amount"] == "50.000"


@pytest.mark.asyncio
async def test_list_all_refunds_endpoint():
    import json
    from app.api.v1.refunds import list_all_refunds
    from app.core.security import TokenPayload

    mock_request = MagicMock()
    mock_request.url.remove_query_params.return_value = "http://testserver/booknpay/api/v1/refunds/"

    mock_user = TokenPayload(
        sub="admin-123",
        user_type="admin",
        role="admin",
        permissions=["*"],
    )

    mock_refund = MagicMock(spec=Refund)
    mock_refund.id = uuid.uuid4()
    mock_refund.refund_number = "REF/2026/10/000001"
    mock_refund.booking_id = uuid.uuid4()
    mock_refund.booking_data = None
    mock_refund.payment_id = uuid.uuid4()
    mock_refund.invoice_number = "INV/2026/10/000001"
    mock_refund.credit_note_number = "RINV/2026/10/000001"
    mock_refund.customer_id = uuid.uuid4()
    mock_refund.customer_data = None
    mock_refund.branch_id = uuid.uuid4()
    mock_refund.branch_data = None
    mock_refund.refund_type = "manual"
    mock_refund.refund_method = "cash"
    mock_refund.status = "completed"
    mock_refund.requested_amount = Decimal("60.000")
    mock_refund.cancellation_fee = Decimal("0.000")
    mock_refund.refunded_amount = Decimal("60.000")
    mock_refund.currency = "KWD"
    mock_refund.payment_gateway = None
    mock_refund.gateway_refund_id = None
    mock_refund.gateway_transaction_id = None
    mock_refund.reason = "Branch customer request"
    mock_refund.notes = "Processed in cash"
    mock_refund.customer_confirmation = None
    mock_refund.reference_number = None
    mock_refund.processed_by = "admin-123"
    mock_refund.processed_by_data = {}
    mock_refund.processed_at = datetime.now(timezone.utc)
    mock_refund.created_at = datetime.now(timezone.utc)
    mock_refund.updated_at = datetime.now(timezone.utc)

    mock_service = AsyncMock()
    mock_service.list_refunds.return_value = (
        [mock_refund],
        1,
        {
            "total_refunded_amount": "60.000",
            "total_cancellation_fee": "0.000",
            "total_requested_amount": "60.000",
            "total_count": 1,
        },
    )

    response = await list_all_refunds(
        request=mock_request,
        current_user=mock_user,
        refund_service=mock_service,
        page=1,
        page_size=20,
    )

    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["success"] is True
    assert len(data["data"]) == 1
    assert data["data"][0]["refund_number"] == "REF/2026/10/000001"
    assert data["data"][0]["invoice_number"] == "INV/2026/10/000001"
    assert data["data"][0]["credit_note_number"] == "RINV/2026/10/000001"
    assert data["meta"]["pagination"]["count"] == 1
    assert data["meta"]["pagination"]["total_pages"] == 1
    assert data["analytics"]["total_refunded_amount"] == "60.000"

