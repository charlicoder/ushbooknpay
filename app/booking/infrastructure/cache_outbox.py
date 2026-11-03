"""
app/booking/infrastructure/cache_outbox.py
───────────────────────────────────────────
Durable retry queue ("outbox") for appointment-cache calls made to ushauth.

Why
---
A booking lives in the ushbooknpay database while its availability rows live
in ushauth (``ush_spa_appointment_cache``).  The two writes cannot share a
transaction, so a transient ushauth outage used to leave a booking with no
availability row (the slot could then be double-booked).

How
---
``USHAuthClient`` still calls ushauth synchronously.  When that call fails with
a *retryable* error the exact request is persisted here and replayed by
``cache_outbox_relay`` with exponential backoff until ushauth acknowledges it.
Every ushauth cache endpoint is idempotent (upsert / delete / status set), so
at-least-once delivery is safe.  Rows of the same booking are replayed strictly
in creation order so a ``create`` is always applied before a later ``status``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

PENDING = "pending"
DELIVERED = "delivered"
DEAD = "dead"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AppointmentCacheOutbox(Base):
    """One pending (or finished) ushauth cache request."""

    __tablename__ = "appointment_cache_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_cache_outbox_due", "status", "next_attempt_at"),
        Index("ix_cache_outbox_booking", "booking_id", "created_at"),
    )
