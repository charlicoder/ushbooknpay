"""
app/payment/infrastructure/repository.py
────────────────────────────────────────
Database operations and sequence generators for Payment records.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.payment.infrastructure.models import Payment


async def generate_payment_number(
    session: AsyncSession, for_date: date | None = None
) -> str:
    """
    Generate the next unique sequential payment number for the given date.

    Format: PMT/YYYY/MM/{NNNNNN}
    Example: PMT/2026/10/000001 → first payment in October 2026

    The counter (NNNNNN) is the count of payments that already have a
    payment_number assigned for that calendar month + 1.
    """
    ref_date = for_date or datetime.now(tz=timezone.utc).date()
    date_prefix = f"PMT/{ref_date.strftime('%Y/%m')}/"

    stmt = select(func.count(Payment.id)).where(
        Payment.payment_number.like(f"{date_prefix}%")
    )
    result = await session.execute(stmt)
    try:
        existing_val = result.scalar_one() if hasattr(result, "scalar_one") else 0
        if hasattr(existing_val, "__await__"):
            existing_val = await existing_val
        existing_count = int(existing_val)
    except Exception:
        existing_count = 0

    sequence = existing_count + 1
    return f"{date_prefix}{sequence:06d}"


class PaymentRepository:
    """Async repository for Payment aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def generate_payment_number(self, for_date: date | None = None) -> str:
        return await generate_payment_number(self._session, for_date)
