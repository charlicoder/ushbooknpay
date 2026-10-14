"""
tests/unit/shop/test_shop_delivery_status.py
─────────────────────────────────────────────
Verify delivery status transitions, including staff transition to 'received'.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.shop.application.services import ShopOrderService
from app.shop.domain.state_machine import DeliveryStateMachine
from app.shop.domain.value_objects import DeliveryStatus
from app.shop.infrastructure.models import ShopOrder
from app.shop.interfaces.schemas import UpdateDeliveryStatusRequest


def test_delivery_state_machine_staff_can_transition_to_received():
    """Verify that staff can transition from DELIVERED to RECEIVED."""
    sm = DeliveryStateMachine(DeliveryStatus.DELIVERED)
    assert sm.can_transition(DeliveryStatus.RECEIVED)
    sm.assert_staff_can_transition(DeliveryStatus.RECEIVED)


def test_update_delivery_status_request_accepts_received():
    """Verify UpdateDeliveryStatusRequest accepts received status without error."""
    req = UpdateDeliveryStatusRequest(status=DeliveryStatus.RECEIVED, note="Confirmed")
    assert req.status == DeliveryStatus.RECEIVED
    assert req.note == "Confirmed"


@pytest.mark.asyncio
async def test_shop_order_service_update_delivery_status_to_received():
    """Verify ShopOrderService allows staff to update delivery status to received."""
    mock_repo = AsyncMock()
    mock_session = AsyncMock()

    order_id = uuid.uuid4()
    order = ShopOrder(
        id=order_id,
        order_number="ORD/2026/10/000001",
        customer_id=uuid.uuid4(),
        subtotal=10.0,
        total_amount=10.0,
        delivery_status=DeliveryStatus.DELIVERED.value,
    )
    mock_repo.get_by_id.return_value = order

    svc = ShopOrderService(session=mock_session)
    svc._repo = mock_repo
    updated = await svc.update_delivery_status(
        order_id=order_id,
        new_status=DeliveryStatus.RECEIVED,
        changed_by="staff:admin-1",
        note="Customer confirmed receipt",
    )

    assert updated.delivery_status == DeliveryStatus.RECEIVED.value
    mock_repo.update.assert_awaited_once_with(order)
    mock_repo.add_status_history.assert_awaited_once()
    mock_session.commit.assert_awaited_once()
