"""Unit tests for created_by_user and created_by_user_data in payments.

Tests cover:
- Payment ORM model column definitions and backwards-compatible created_by property.
- CreatePaymentRequestSchema / UpdatePaymentRequestSchema parsing, validation, and syncing.
- PaymentListItem / PaymentDetailResponse mapping and serialization.
- Router endpoint handlers (create_payment, update_payment, list_payments).
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.api.v1.payments import (
    _payment_to_detail,
    _payment_to_list_item,
    create_payment,
    list_payments,
    update_payment,
)
from app.payment.infrastructure.models import Payment
from app.payment.interfaces.schemas import (
    CreatePaymentRequestSchema,
    PaymentDetailResponse,
    PaymentListItem,
    UpdatePaymentRequestSchema,
)


def test_payment_model_created_by_user_fields():
    """Verify Payment model stores created_by_user and created_by_user_data."""
    cust_id = uuid.uuid4()
    p = Payment(
        customer_id=cust_id,
        total_amount=Decimal("25.000"),
        total_duration=60,
        currency="KWD",
        created_by_user="user-12345",
        created_by_user_data={"id": "user-12345", "name": "Admin User", "role": "admin"},
    )

    assert p.created_by_user == "user-12345"
    assert p.created_by == "user-12345"
    assert p.created_by_user_data == {"id": "user-12345", "name": "Admin User", "role": "admin"}

    # Test backward compatibility setter
    p.created_by = "user-67890"
    assert p.created_by_user == "user-67890"
    assert p.created_by == "user-67890"


def test_create_payment_schema_with_created_by_user():
    """Verify CreatePaymentRequestSchema accepts created_by_user and syncs created_by."""
    cust_id = uuid.uuid4()
    req = CreatePaymentRequestSchema(
        customer_id=cust_id,
        total_amount=Decimal("15.500"),
        total_duration=45,
        currency="KWD",
        created_by_user="user-creator-1",
        created_by_user_data={"name": "Creator Staff", "email": "staff@ushspa.com"},
    )

    assert req.created_by_user == "user-creator-1"
    assert req.created_by == "user-creator-1"
    assert req.created_by_user_data == {"name": "Creator Staff", "email": "staff@ushspa.com"}


def test_create_payment_schema_with_created_by_alias():
    """Verify CreatePaymentRequestSchema accepts created_by and syncs created_by_user."""
    cust_id = uuid.uuid4()
    req = CreatePaymentRequestSchema(
        customer_id=cust_id,
        total_amount=Decimal("15.500"),
        total_duration=45,
        currency="KWD",
        created_by="user-legacy-creator",
        created_by_user_data={"name": "Legacy Creator"},
    )

    assert req.created_by_user == "user-legacy-creator"
    assert req.created_by == "user-legacy-creator"
    assert req.created_by_user_data == {"name": "Legacy Creator"}


def test_update_payment_schema_with_created_by_user():
    """Verify UpdatePaymentRequestSchema accepts created_by_user and created_by_user_data."""
    req = UpdatePaymentRequestSchema(
        created_by_user="user-updater-1",
        created_by_user_data={"name": "Updater Staff"},
    )

    assert req.created_by_user == "user-updater-1"
    assert req.created_by == "user-updater-1"
    assert req.created_by_user_data == {"name": "Updater Staff"}


def test_payment_detail_and_list_item_mapping():
    """Verify _payment_to_detail and _payment_to_list_item map created_by_user and created_by_user_data."""
    now = datetime.now(timezone.utc)
    p = Payment(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        total_amount=Decimal("30.000"),
        total_duration=60,
        currency="KWD",
        status="success",
        created_at=now,
        created_by_user="creator-uuid-99",
        created_by_user_data={"name": "Finance Admin", "dept": "Accounting"},
    )

    detail = _payment_to_detail(p)
    assert detail.created_by_user == "creator-uuid-99"
    assert detail.created_by == "creator-uuid-99"
    assert detail.created_by_user_data == {"name": "Finance Admin", "dept": "Accounting"}

    list_item = _payment_to_list_item(p)
    assert list_item.created_by_user == "creator-uuid-99"
    assert list_item.created_by == "creator-uuid-99"
    assert list_item.created_by_user_data == {"name": "Finance Admin", "dept": "Accounting"}


@pytest.mark.asyncio
async def test_create_payment_endpoint_populates_created_by_user():
    """Verify create_payment endpoint creates Payment record with created_by_user and created_by_user_data."""
    cust_id = uuid.uuid4()
    body = CreatePaymentRequestSchema(
        customer_id=cust_id,
        total_amount=Decimal("50.000"),
        total_duration=60,
        currency="KWD",
        created_by_user="user-endpoint-creator",
        created_by_user_data={"name": "Receptionist 1", "branch": "Salmiya"},
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    settings = MagicMock()

    res = await create_payment(
        body=body,
        _=MagicMock(),
        session=session,
        settings=settings,
    )

    import json
    content = json.loads(res.body.decode())
    assert content["success"] is True
    data = content["data"]
    assert data["created_by_user"] == "user-endpoint-creator"
    assert data["created_by"] == "user-endpoint-creator"
    assert data["created_by_user_data"] == {"name": "Receptionist 1", "branch": "Salmiya"}

    # Check the Payment object added to session
    added_payment = session.add.call_args_list[0][0][0]
    assert isinstance(added_payment, Payment)
    assert added_payment.created_by_user == "user-endpoint-creator"
    assert added_payment.created_by_user_data == {"name": "Receptionist 1", "branch": "Salmiya"}


@pytest.mark.asyncio
async def test_update_payment_endpoint_updates_created_by_user():
    """Verify update_payment endpoint updates created_by_user and created_by_user_data."""
    payment_id = uuid.uuid4()
    mock_payment = Payment(
        id=payment_id,
        customer_id=uuid.uuid4(),
        total_amount=Decimal("40.000"),
        total_duration=60,
        currency="KWD",
        status="pending",
        created_at=datetime.now(timezone.utc),
        created_by_user="initial-user",
        created_by_user_data={"name": "Initial User"},
    )

    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = mock_payment
    session.execute.return_value = exec_result
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    current_user = MagicMock()
    settings = MagicMock()

    body = UpdatePaymentRequestSchema(
        created_by_user="updated-user-999",
        created_by_user_data={"name": "Updated Staff"},
    )

    res = await update_payment(
        payment_id=payment_id,
        body=body,
        current_user=current_user,
        session=session,
        settings=settings,
    )

    import json
    content = json.loads(res.body.decode())
    assert content["success"] is True
    data = content["data"]
    assert data["created_by_user"] == "updated-user-999"
    assert data["created_by"] == "updated-user-999"
    assert data["created_by_user_data"] == {"name": "Updated Staff"}
    assert mock_payment.created_by_user == "updated-user-999"
    assert mock_payment.created_by_user_data == {"name": "Updated Staff"}


@pytest.mark.asyncio
async def test_list_payments_filters_by_created_by_user():
    """Verify list_payments queries Payment.created_by_user when filter is provided."""
    session = AsyncMock()

    # Total count mock
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    count_result.scalar.return_value = 1

    # Analytics mock
    analytics_result = MagicMock()
    analytics_row = MagicMock()
    analytics_row.total_count = 1
    analytics_row.total_revenue = Decimal("100.000")
    analytics_row.successful_count = 1
    analytics_row.successful_revenue = Decimal("100.000")
    analytics_row.failed_count = 0
    analytics_row.pending_count = 0
    analytics_row.refunded_count = 0
    analytics_row.refunded_amount = Decimal("0.000")
    analytics_result.one.return_value = analytics_row

    # Items mock
    mock_p = Payment(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        total_amount=Decimal("100.000"),
        total_duration=60,
        currency="KWD",
        status="success",
        created_at=datetime.now(timezone.utc),
        created_by_user="filter-user-1",
        created_by_user_data={"name": "Filter User"},
    )
    items_result = MagicMock()
    items_result.scalars.return_value.all.return_value = [mock_p]

    session.execute.side_effect = [count_result, analytics_result, items_result]

    res = await list_payments(
        current_user=MagicMock(),
        session=session,
        request=MagicMock(),
        page=1,
        page_size=20,
        created_by_user_filter="filter-user-1",
    )

    import json
    content = json.loads(res.body.decode())
    assert content["success"] is True
    assert len(content["data"]) == 1
    assert content["data"][0]["created_by_user"] == "filter-user-1"
    assert content["data"][0]["created_by"] == "filter-user-1"
    assert content["data"][0]["created_by_user_data"] == {"name": "Filter User"}
