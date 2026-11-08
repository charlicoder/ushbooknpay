"""
tests/unit/course/helpers.py
────────────────────────────
In-memory fakes for the course-subscription unit tests (repo mock style: no DB).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.course.application.services import CourseSubscriptionService
from app.course.domain.value_objects import CourseUsageStatus
from app.course.infrastructure.models import CourseSessionUsage, CourseSubscription

SERVICE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
COURSE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def now() -> datetime:
    return datetime.now(timezone.utc)


def course_payload(**overrides: Any) -> dict[str, Any]:
    data = {
        "id": str(COURSE_ID),
        "code": "COURSE-001",
        "name": "Relaxation Package",
        "name_en": "Relaxation Package",
        "name_ar": "باقة الاسترخاء",
        "service_id": str(SERVICE_ID),
        "service": {
            "id": str(SERVICE_ID),
            "name": "Relaxing Massage",
            "duration_minutes": 60,
            "base_price": "20.000",
            "status": "active",
        },
        "arrangement_type": "vip_suite",
        "arrangement_type_label": "VIP Suite",
        "total_sessions": 5,
        "price": "0.000",
        "currency": "KWD",
        "unit_price": "20.000",
        "effective_price": "100.000",
        "is_auto_priced": True,
        "validity_days": 90,
        "status": "active",
    }
    data.update(overrides)
    return data


def make_sub(**overrides: Any) -> CourseSubscription:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "subscription_number": "CRS/2026/11/000001",
        "customer_id": uuid.uuid4(),
        "customer_data": {"id": "c", "name": "Dana Test", "phone_number": "+96512345678"},
        "course_id": COURSE_ID,
        "course_data": {
            "id": str(COURSE_ID),
            "code": "COURSE-001",
            "name": "Relaxation Package",
            "service_name": "Relaxing Massage",
            "service_duration_minutes": 60,
        },
        "service_id": SERVICE_ID,
        "arrangement_type": "vip_suite",
        "total_sessions": 5,
        "used_sessions": 0,
        "reserved_sessions": 0,
        "validity_days": 90,
        "price": Decimal("100.000"),
        "currency": "KWD",
        "status": "active",
        "purchased_at": now() - timedelta(days=1),
        "activated_at": now() - timedelta(days=1),
        "expires_at": now() + timedelta(days=89),
        "invoice_status": "created",
        "invoice_attempts": 0,
        "payment_option": "pay_at_desk",
    }
    values.update(overrides)
    return CourseSubscription(**values)


def make_booking(sub: CourseSubscription | None = None, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "booking_number": "BOK/2026/11/000010",
        "customer_id": sub.customer_id if sub else uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "branch_data": {"name": "Salmiya"},
        "therapist_id": uuid.uuid4(),
        "therapist_data": {"name": "Farida"},
        "service_id": SERVICE_ID,
        "service_data": {"name": "Relaxing Massage"},
        "service_arrangement_id": uuid.uuid4(),
        "service_arrangement_data": {"arrangement_name": "VIP 1", "arrangement_type": "vip_suite"},
        "services": [],
        "sessions": [],
        "addons": [],
        "extra_minutes": 0,
        "appointment_start": now() + timedelta(days=2),
        "appointment_end": now() + timedelta(days=2, hours=1),
        "booking_type": "course_redemption",
        "payment_type": "course_subscription",
        "course_subscription_id": sub.id if sub else None,
        "course_data": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeRepo:
    """Implements the CourseSubscriptionRepository API used by the service."""

    def __init__(self, *subs: CourseSubscription) -> None:
        self.subs: dict[uuid.UUID, CourseSubscription] = {s.id: s for s in subs}
        self.usages: dict[uuid.UUID, CourseSessionUsage] = {}
        self.locked: list[uuid.UUID] = []
        self.flushes = 0
        self.number_seq = 0

    @property
    def session(self) -> Any:  # pragma: no cover
        return None

    async def generate_subscription_number(self, for_date: Any = None) -> str:
        self.number_seq += 1
        return f"CRS/2026/11/{self.number_seq:06d}"

    async def get(self, subscription_id: uuid.UUID, *, for_update: bool = False) -> CourseSubscription | None:
        if for_update:
            self.locked.append(subscription_id)
        return self.subs.get(subscription_id)

    async def get_by_idempotency_key(self, key: str) -> CourseSubscription | None:
        return next((s for s in self.subs.values() if s.idempotency_key == key), None)

    async def get_usage_by_booking(self, booking_id: uuid.UUID, *, for_update: bool = False) -> CourseSessionUsage | None:
        return next((u for u in self.usages.values() if u.booking_id == booking_id), None)

    async def list_usages(self, subscription_id: uuid.UUID) -> list[CourseSessionUsage]:
        return sorted(
            (u for u in self.usages.values() if u.subscription_id == subscription_id),
            key=lambda u: u.session_number,
        )

    async def active_session_numbers(self, subscription_id: uuid.UUID) -> list[int]:
        return [
            u.session_number
            for u in self.usages.values()
            if u.subscription_id == subscription_id and u.status != CourseUsageStatus.RELEASED.value
        ]

    async def claim_due_for_expiry(self, now_: datetime, limit: int = 100) -> list[CourseSubscription]:
        return [s for s in self.subs.values() if s.status == "active" and s.expires_at and s.expires_at <= now_]

    async def claim_invoice_retries(self, limit: int = 50, max_attempts: int = 20) -> list[CourseSubscription]:
        return [
            s
            for s in self.subs.values()
            if s.status in ("active", "exhausted", "expired")
            and s.invoice_status in ("failed", "pending")
            and (s.invoice_attempts or 0) < max_attempts
        ]

    def add(self, obj: Any) -> Any:
        if isinstance(obj, CourseSubscription):
            self.subs[obj.id] = obj
        elif isinstance(obj, CourseSessionUsage):
            self.usages[obj.id] = obj
        return obj

    async def flush(self) -> None:
        self.flushes += 1
        for s in self.subs.values():
            # mirror of the DB check constraints
            assert s.used_sessions >= 0 and s.reserved_sessions >= 0
            assert s.used_sessions + s.reserved_sessions <= s.total_sessions


class FakeUshanr:
    def __init__(self, invoice: dict[str, str] | None = None, credit_note: dict[str, Any] | None = None) -> None:
        self.invoice = invoice
        self.credit_note = credit_note
        self.invoice_calls = 0
        self.credit_note_calls = 0

    async def create_or_get_course_subscription_invoice_data(self, **kwargs: Any) -> dict[str, str] | None:
        self.invoice_calls += 1
        return self.invoice

    async def create_credit_note(self, **kwargs: Any) -> dict[str, Any] | None:
        self.credit_note_calls += 1
        self.credit_note_kwargs = kwargs
        return self.credit_note

    async def aclose(self) -> None:
        return None


def make_service(
    *subs: CourseSubscription,
    ushanr: FakeUshanr | None = None,
    ushauth: Any = None,
) -> tuple[CourseSubscriptionService, FakeRepo, MagicMock, list[Any]]:
    """Service wired to a FakeRepo; returns (service, repo, session, emitted_events)."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    fake_ushanr = ushanr or FakeUshanr(invoice={"invoice_id": "inv-1", "invoice_number": "INV/2026/11/00001"})
    service = CourseSubscriptionService(
        session,
        ushauth_client=ushauth,
        ushanr_factory=lambda: fake_ushanr,
    )
    repo = FakeRepo(*subs)
    service._repo = repo  # type: ignore[assignment]
    events: list[Any] = []
    service._emit = lambda event, sub: events.append(event)  # type: ignore[method-assign]
    return service, repo, session, events

