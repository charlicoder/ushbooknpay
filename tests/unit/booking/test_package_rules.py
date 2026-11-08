"""Package (component-service) booking rules."""

import uuid
from decimal import Decimal

import pytest

from app.booking.application.package_rules import apply_package_context, resolve_package_context
from app.booking.domain.sessions import SessionValidationError
from app.booking.interfaces.schemas import BookingServiceItem


class _Item:
    def __init__(self, services):
        self.services = services


class _FakeAuth:
    def __init__(self, pkg):
        self.pkg = pkg

    async def get_service(self, sid):
        return {"data": self.pkg}


def _svc(sid, pkg):
    return BookingServiceItem(serviceId=sid, packageId=pkg)


def _pkg(pid, comps, price="66.000", is_pkg=True):
    return {
        "id": str(pid),
        "name": "ROYAL",
        "is_packaged_services": is_pkg,
        "base_price": price,
        "included_services": [
            {"id": str(c), "duration_minutes": 30 + i * 10} for i, c in enumerate(comps)
        ],
    }


@pytest.mark.asyncio
async def test_no_package_items_is_noop():
    items = [_Item([BookingServiceItem(serviceId=uuid.uuid4())])]
    assert await resolve_package_context(_FakeAuth({}), items) == {}


@pytest.mark.asyncio
async def test_all_in_one_session_and_split_are_valid():
    pid, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    auth = _FakeAuth(_pkg(pid, [a, b]))
    one = await resolve_package_context(auth, [_Item([_svc(a, pid), _svc(b, pid)])])
    split = await resolve_package_context(auth, [_Item([_svc(a, pid)]), _Item([_svc(b, pid)])])
    assert one[str(pid)]["carrier"] == (0, 0)
    assert split[str(pid)]["carrier"] == (0, 0)
    assert one[str(pid)]["price"] == Decimal("66.000")


@pytest.mark.asyncio
async def test_missing_or_duplicate_component_rejected():
    pid, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    auth = _FakeAuth(_pkg(pid, [a, b]))
    with pytest.raises(SessionValidationError):
        await resolve_package_context(auth, [_Item([_svc(a, pid)])])
    with pytest.raises(SessionValidationError):
        await resolve_package_context(auth, [_Item([_svc(a, pid), _svc(a, pid), _svc(b, pid)])])


@pytest.mark.asyncio
async def test_non_package_rejected():
    pid, a = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(SessionValidationError):
        await resolve_package_context(_FakeAuth(_pkg(pid, [a], is_pkg=False)), [_Item([_svc(a, pid)])])


@pytest.mark.asyncio
async def test_package_price_charged_once_on_first_component():
    pid, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = await resolve_package_context(
        _FakeAuth(_pkg(pid, [a, b])), [_Item([_svc(a, pid)]), _Item([_svc(b, pid)])]
    )
    e0 = [{"service_id": str(a), "base_price": "20.000", "service_data": {}}]
    e1 = [{"service_id": str(b), "base_price": "25.000", "service_data": {}}]
    apply_package_context(e0, [_svc(a, pid)], 0, ctx)
    apply_package_context(e1, [_svc(b, pid)], 1, ctx)
    assert e0[0]["base_price"] == "66.000"
    assert e1[0]["base_price"] == "0.000"
    assert e0[0]["package_name"] == "ROYAL" and e1[0]["service_data"]["package_id"] == str(pid)


@pytest.mark.asyncio
async def test_allow_component_swap_on_session_edit():
    pid, a, b, swapped = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    auth = _FakeAuth(_pkg(pid, [a, b]))
    ctx = await resolve_package_context(
        auth, [_Item([_svc(swapped, pid)]), _Item([_svc(b, pid)])], allow_component_swap=True
    )
    assert ctx[str(pid)]["price"] == Decimal("66.000")
    assert ctx[str(pid)]["carrier"] == (0, 0)
    with pytest.raises(SessionValidationError):
        await resolve_package_context(
            auth, [_Item([_svc(swapped, pid)])], allow_component_swap=True
        )
    with pytest.raises(SessionValidationError):
        await resolve_package_context(
            auth, [_Item([_svc(b, pid)]), _Item([_svc(b, pid)])], allow_component_swap=True
        )

