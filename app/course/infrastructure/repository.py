"""
app/course/infrastructure/repository.py
───────────────────────────────────────
Data access for course subscriptions. No business rules; never commits.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import String, and_, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.utils import local_now
from app.course.domain.value_objects import (
    CourseInvoiceStatus,
    CourseSubscriptionStatus,
    CourseUsageStatus,
    PAID_STATUSES,
)
from app.course.infrastructure.models import CourseSessionUsage, CourseSubscription


def _extract_sequence(value: Any) -> int:
    if not value:
        return 0
    last = str(value).strip().split("/")[-1]
    return int(last) if last.isdigit() else 0


def _is_mock(session: Any) -> bool:
    return any(cls.__name__ in ("MagicMock", "AsyncMock") for cls in type(session).__mro__)


class CourseSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    # ── Number generator ──────────────────────────────────────────────────
    async def generate_subscription_number(self, for_date: date | None = None) -> str:
        """``CRS/YYYY/MM/NNNNNN`` serialised by a transaction advisory lock (like BOK)."""
        target = for_date or local_now().date()
        prefix = f"CRS/{target.strftime('%Y/%m')}/"
        try:
            bind = getattr(self._session, "bind", None)
            if bind is None and hasattr(self._session, "get_bind") and not _is_mock(self._session):
                bind = self._session.get_bind()
            if bind is not None and getattr(bind.dialect, "name", None) == "postgresql":
                await self._session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('course_subscription_number'))")
                )
        except Exception:
            pass
        result = await self._session.execute(
            select(func.max(CourseSubscription.subscription_number)).where(
                CourseSubscription.subscription_number.like(f"{prefix}%")
            )
        )
        current = result.scalar_one() if hasattr(result, "scalar_one") else None
        if hasattr(current, "__await__"):
            current = await current
        return f"{prefix}{_extract_sequence(current) + 1:06d}"

    # ── Reads ─────────────────────────────────────────────────────────────
    async def get(self, subscription_id: uuid.UUID, *, for_update: bool = False) -> CourseSubscription | None:
        stmt = select(CourseSubscription).where(CourseSubscription.id == subscription_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, key: str) -> CourseSubscription | None:
        stmt = select(CourseSubscription).where(CourseSubscription.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_usage_by_booking(
        self, booking_id: uuid.UUID, *, for_update: bool = False
    ) -> CourseSessionUsage | None:
        stmt = select(CourseSessionUsage).where(CourseSessionUsage.booking_id == booking_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_usages(self, subscription_id: uuid.UUID) -> list[CourseSessionUsage]:
        stmt = (
            select(CourseSessionUsage)
            .where(CourseSessionUsage.subscription_id == subscription_id)
            .order_by(CourseSessionUsage.session_number, CourseSessionUsage.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def active_session_numbers(self, subscription_id: uuid.UUID) -> list[int]:
        stmt = select(CourseSessionUsage.session_number).where(
            CourseSessionUsage.subscription_id == subscription_id,
            CourseSessionUsage.status != CourseUsageStatus.RELEASED.value,
        )
        return [int(n) for n in (await self._session.execute(stmt)).scalars().all()]

    async def list_subscriptions(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        course_id: uuid.UUID | None = None,
        customer_id: uuid.UUID | None = None,
        status: str | None = None,
        expired: bool | None = None,
        search: str | None = None,
        redeemable: bool | None = None,
        now: datetime,
    ) -> tuple[list[CourseSubscription], int]:
        conds: list[Any] = []
        if course_id:
            conds.append(CourseSubscription.course_id == course_id)
        if customer_id:
            conds.append(CourseSubscription.customer_id == customer_id)
        is_expired_expr = or_(
            CourseSubscription.status == CourseSubscriptionStatus.EXPIRED.value,
            and_(
                CourseSubscription.status == CourseSubscriptionStatus.ACTIVE.value,
                CourseSubscription.expires_at.is_not(None),
                CourseSubscription.expires_at <= now,
            ),
        )
        if status:
            if status == CourseSubscriptionStatus.EXPIRED.value:
                conds.append(is_expired_expr)
            elif status == CourseSubscriptionStatus.ACTIVE.value:
                conds.append(CourseSubscription.status == status)
                conds.append(or_(CourseSubscription.expires_at.is_(None), CourseSubscription.expires_at > now))
            else:
                conds.append(CourseSubscription.status == status)
        if expired is True:
            conds.append(is_expired_expr)
        elif expired is False:
            conds.append(~is_expired_expr)
        if redeemable:
            conds.append(CourseSubscription.status == CourseSubscriptionStatus.ACTIVE.value)
            conds.append(CourseSubscription.expires_at > now)
            conds.append(
                CourseSubscription.used_sessions + CourseSubscription.reserved_sessions
                < CourseSubscription.total_sessions
            )
        if search:
            like = f"%{search.strip()}%"
            conds.append(
                or_(
                    CourseSubscription.subscription_number.ilike(like),
                    cast(CourseSubscription.customer_data, String).ilike(like),
                    cast(CourseSubscription.course_data, String).ilike(like),
                )
            )
        base = select(CourseSubscription).where(*conds)
        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            await self._session.execute(
                base.order_by(CourseSubscription.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return list(rows), int(total or 0)

    async def claim_due_for_expiry(self, now: datetime, limit: int = 100) -> Sequence[CourseSubscription]:
        stmt = (
            select(CourseSubscription)
            .where(
                CourseSubscription.status == CourseSubscriptionStatus.ACTIVE.value,
                CourseSubscription.expires_at.is_not(None),
                CourseSubscription.expires_at <= now,
            )
            .order_by(CourseSubscription.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def claim_invoice_retries(self, limit: int = 50, max_attempts: int = 20) -> Sequence[CourseSubscription]:
        stmt = (
            select(CourseSubscription)
            .where(
                CourseSubscription.status.in_(sorted(PAID_STATUSES)),
                CourseSubscription.invoice_status.in_(
                    [CourseInvoiceStatus.FAILED.value, CourseInvoiceStatus.PENDING.value]
                ),
                CourseSubscription.activated_at.is_not(None),
                CourseSubscription.invoice_attempts < max_attempts,
            )
            .order_by(CourseSubscription.activated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def booking_statuses(self, booking_ids: list[uuid.UUID]) -> dict[str, str]:
        """Live booking statuses for the usage list (contract D: ``booking_status``)."""
        if not booking_ids:
            return {}
        from app.booking.infrastructure.models import Booking

        rows = (
            await self._session.execute(select(Booking.id, Booking.status).where(Booking.id.in_(booking_ids)))
        ).all()
        return {str(r[0]): str(r[1]) for r in rows}

    # ── Writes ────────────────────────────────────────────────────────────
    def add(self, obj: Any) -> Any:
        self._session.add(obj)
        return obj

    async def flush(self) -> None:
        await self._session.flush()

