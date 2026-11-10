"""Therapist lock-down on the bookings API: own bookings only, read-only, → completed only."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1 import bookings as api
from app.core.security import TokenPayload

THERAPIST_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()


def _therapist() -> TokenPayload:
    return TokenPayload(
        sub=str(THERAPIST_ID),
        user_type="employee",
        role="Therapist",
        permissions={"is_therapist_role": True},
        codenames=[],
    )


def _admin() -> TokenPayload:
    return TokenPayload(sub=str(OTHER_ID), user_type="employee", role="Administrator", is_superuser=True)


def _booking(therapist=THERAPIST_ID, secondary=None):
    return SimpleNamespace(therapist_id=therapist, secondary_therapist_id=secondary, sessions=[])


def test_is_therapist_user():
    assert api._is_therapist_user(_therapist())
    assert not api._is_therapist_user(_admin())
    assert not api._is_therapist_user(None)


def test_booking_involves_therapist_primary_secondary_and_other():
    assert api._booking_involves_therapist(_booking(), str(THERAPIST_ID))
    assert api._booking_involves_therapist(_booking(OTHER_ID, THERAPIST_ID), str(THERAPIST_ID))
    assert not api._booking_involves_therapist(_booking(OTHER_ID), str(THERAPIST_ID))


def test_deny_therapist_blocks_therapist_only():
    with pytest.raises(HTTPException) as exc:
        api._deny_therapist(_therapist())
    assert exc.value.status_code == 403
    api._deny_therapist(_admin())


@pytest.mark.asyncio
async def test_get_booking_forbidden_for_other_therapists_booking():
    svc = MagicMock()
    svc.get_booking = AsyncMock(return_value=_booking(OTHER_ID))
    with pytest.raises(HTTPException) as exc:
        await api.get_booking(uuid.uuid4(), None, svc, current_user=_therapist())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_status_update_rejects_non_completed_for_therapist():
    svc = MagicMock()
    body = SimpleNamespace(status="cancelled")
    with pytest.raises(HTTPException) as exc:
        await api.update_booking_status(
            uuid.uuid4(), body, None, svc, MagicMock(), request=None, current_user=_therapist()
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_status_update_rejects_foreign_booking_for_therapist():
    svc = MagicMock()
    svc.get_booking = AsyncMock(return_value=_booking(OTHER_ID))
    body = SimpleNamespace(status="completed")
    with pytest.raises(HTTPException) as exc:
        await api.update_booking_status(
            uuid.uuid4(), body, None, svc, MagicMock(), request=None, current_user=_therapist()
        )
    assert exc.value.status_code == 403


def test_therapist_window_is_today_to_plus_7():
    from datetime import date

    start, end = api._therapist_window_dates()
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 7



def test_therapist_key_prefers_profile_id_over_user_id():
    profile_id = str(uuid.uuid4())
    user = TokenPayload(
        sub=str(THERAPIST_ID), user_type="employee", role="Therapist",
        permissions={"is_therapist_role": True, "therapist_id": profile_id},
    )
    assert api._therapist_key(user) == profile_id
    assert api._therapist_key(_therapist()) == str(THERAPIST_ID)
