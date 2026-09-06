"""
app/events/outbox/models.py
────────────────────────────
Transactional Outbox pattern — ORM model.

The outbox table holds domain events that need to be published to SQS.
Events are written inside the same database transaction as the state change.
A background publisher process reads unpublished events and sends them to SQS.

This guarantees at-least-once delivery and prevents lost events even if
the service crashes between the state change and the SQS publish.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OutboxEvent(Base):
    """
    Outbox event record.

    Lifecycle:
        PENDING  → PROCESSING → PUBLISHED (terminal)
                             → FAILED     (terminal)
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Event Metadata ────────────────────────────────────────────────────
    event_name: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # e.g. "Booking.Confirmed"
    queue_url: Mapped[str] = mapped_column(String(512), nullable=False)
    message_group_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # For FIFO queues

    # ── Payload ───────────────────────────────────────────────────────────
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # ── Status ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending | processing | published | failed

    # ── Retry Tracking ────────────────────────────────────────────────────
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timing ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )  # Allows delayed publishing / exponential backoff
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Correlation ───────────────────────────────────────────────────────
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # Publisher polls on this index
        Index(
            "ix_outbox_events_status_scheduled",
            "status",
            "scheduled_at",
        ),
        # For idempotency lookups
        Index("ix_outbox_events_event_name", "event_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<OutboxEvent id={self.id} event={self.event_name} status={self.status}>"
        )
