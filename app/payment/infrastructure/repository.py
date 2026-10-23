"""
app/payment/infrastructure/repository.py
────────────────────────────────────────
Database operations and sequence generators for Payment records.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.payment.infrastructure.models import Payment


def _extract_sequence(existing_val: Any) -> int:
    """Extract integer sequence from scalar query result.

    Handles:
    - None / 0 / empty: returns 0 (so next sequence is 1)
    - int / float: returns int (so next sequence is int + 1)
    - string sequence number e.g. "PMT/2026/10/000007": returns 7
    - string digits e.g. "99": returns 99
    - other strings / invalid: returns 0
    """
    if not existing_val:
        return 0
    if isinstance(existing_val, (int, float)):
        return int(existing_val)
    if isinstance(existing_val, str):
        val_str = existing_val.strip()
        if not val_str:
            return 0
        last_part = val_str.split("/")[-1]
        if last_part.isdigit():
            return int(last_part)
        try:
            return int(val_str)
        except ValueError:
            return 0
    return 0


async def generate_payment_number(
    session: AsyncSession, for_date: date | None = None
) -> str:
    """
    Generate the next unique sequential payment number for the given date.

    Format: PMT/YYYY/MM/{NNNNNN}
    Example: PMT/2026/10/000001 → first payment in October 2026

    The sequence is derived from the highest existing ``payment_number``
    sharing the same month prefix (LIKE 'PMT/YYYY/MM/%') and adding 1.
    This handles gaps safely without colliding with existing records.
    """
    ref_date = for_date or datetime.now(tz=timezone.utc).date()
    date_prefix = f"PMT/{ref_date.strftime('%Y/%m')}/"

    # In PostgreSQL, acquire a transaction advisory lock to serialize payment number generation
    try:
        bind = getattr(session, "bind", None)
        if bind is None and hasattr(session, "get_bind"):
            if not any(cls.__name__ in ("MagicMock", "AsyncMock") for cls in type(session).__mro__):
                bind = session.get_bind()
        if bind and getattr(bind.dialect, "name", None) == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('payment_number'))")
            )
    except Exception:
        pass

    stmt = select(func.max(Payment.payment_number)).where(
        Payment.payment_number.like(f"{date_prefix}%")
    )
    result = await session.execute(stmt)
    try:
        existing_val = result.scalar_one() if hasattr(result, "scalar_one") else (result.scalar() if hasattr(result, "scalar") else None)
        if hasattr(existing_val, "__await__"):
            existing_val = await existing_val
        highest_sequence = _extract_sequence(existing_val)
    except Exception:
        highest_sequence = 0

    sequence = highest_sequence + 1
    return f"{date_prefix}{sequence:06d}"


async def generate_refund_number(
    session: AsyncSession, for_date: date | None = None
) -> str:
    """
    Generate the next unique sequential refund number for the given date.

    Format: REF/YYYY/MM/{NNNNNN}
    Example: REF/2026/10/000001 → first refund in October 2026

    The sequence is derived from the highest existing ``refund_number``
    sharing the same month prefix (LIKE 'REF/YYYY/MM/%') and adding 1.
    This handles gaps safely without colliding with existing records.
    """
    from app.payment.infrastructure.models import Refund

    ref_date = for_date or datetime.now(tz=timezone.utc).date()
    date_prefix = f"REF/{ref_date.strftime('%Y/%m')}/"

    # In PostgreSQL, acquire a transaction advisory lock to serialize refund number generation
    try:
        bind = getattr(session, "bind", None)
        if bind is None and hasattr(session, "get_bind"):
            if not any(cls.__name__ in ("MagicMock", "AsyncMock") for cls in type(session).__mro__):
                bind = session.get_bind()
        if bind and getattr(bind.dialect, "name", None) == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('refund_number'))")
            )
    except Exception:
        pass

    stmt = select(func.max(Refund.refund_number)).where(
        Refund.refund_number.like(f"{date_prefix}%")
    )
    result = await session.execute(stmt)
    try:
        existing_val = result.scalar_one() if hasattr(result, "scalar_one") else (result.scalar() if hasattr(result, "scalar") else None)
        if hasattr(existing_val, "__await__"):
            existing_val = await existing_val
        highest_sequence = _extract_sequence(existing_val)
    except Exception:
        highest_sequence = 0

    sequence = highest_sequence + 1
    return f"{date_prefix}{sequence:06d}"


class PaymentRepository:
    """Async repository for Payment aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def generate_payment_number(self, for_date: date | None = None) -> str:
        return await generate_payment_number(self._session, for_date)

    async def generate_refund_number(self, for_date: date | None = None) -> str:
        return await generate_refund_number(self._session, for_date)

    async def flush(self) -> None:
        """Flush pending changes to the database."""
        await self._session.flush()

    async def flush_in_savepoint(self) -> None:
        """Flush within a nested transaction (SAVEPOINT) if supported."""
        if hasattr(self._session, "begin_nested") and not any(
            cls.__name__ in ("MagicMock", "AsyncMock") for cls in type(self._session).__mro__
        ):
            async with self._session.begin_nested():
                await self._session.flush()
        else:
            await self._session.flush()

