"""
app/course/infrastructure/models.py
───────────────────────────────────
ORM models for course subscriptions (created by migration 0063).

* ``course_subscriptions``  – one row per purchase, holds the session balance.
* ``course_session_usages`` – one row per redemption booking (reserved → consumed | released).

The DB check ``used_sessions + reserved_sessions <= total_sessions`` is the last
line of defence against over-booking; the service always locks the subscription
row (``SELECT … FOR UPDATE``) before changing the counters.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.course.domain.value_objects import (
    CourseInvoiceStatus,
    CourseSubscriptionStatus,
    CourseUsageStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CourseSubscription(Base):
    __tablename__ = "course_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # CRS/YYYY/MM/NNNNNN
    subscription_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    # Course snapshot taken at purchase time (existing subscriptions keep their terms)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    course_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    arrangement_type: Mapped[str] = mapped_column(String(50), nullable=False)

    total_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    used_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    validity_days: Mapped[int] = mapped_column(Integer, nullable=False)

    price: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False, default=Decimal("0.000"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CourseSubscriptionStatus.PAYMENT_PENDING.value
    )

    purchased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Payment ──────────────────────────────────────────────────────────
    payment_option: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payment_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payment_gateway: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payment_through: Mapped[str | None] = mapped_column(String(20), nullable=True)
    transaction_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_link_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Invoice (ushanr) ─────────────────────────────────────────────────
    invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    invoice_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CourseInvoiceStatus.PENDING.value, server_default="pending"
    )
    invoice_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    invoice_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Cancel / refund ──────────────────────────────────────────────────
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    refund_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    refund_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── Audit ────────────────────────────────────────────────────────────
    sold_by_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sold_by_user_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_course_subscriptions_idempotency_key"),
        CheckConstraint("total_sessions > 0", name="ck_course_subscriptions_total_positive"),
        CheckConstraint("used_sessions >= 0", name="ck_course_subscriptions_used_non_negative"),
        CheckConstraint("reserved_sessions >= 0", name="ck_course_subscriptions_reserved_non_negative"),
        CheckConstraint(
            "used_sessions + reserved_sessions <= total_sessions",
            name="ck_course_subscriptions_balance",
        ),
        CheckConstraint("price >= 0", name="ck_course_subscriptions_price_non_negative"),
        CheckConstraint(
            "status IN ('payment_pending', 'payment_failed', 'active', 'exhausted', 'expired', "
            "'cancelled', 'refunded')",
            name="ck_course_subscriptions_status",
        ),
        CheckConstraint(
            "invoice_status IN ('pending', 'created', 'failed')",
            name="ck_course_subscriptions_invoice_status",
        ),
        Index("ix_course_subscriptions_customer_id", "customer_id"),
        Index("ix_course_subscriptions_course_id", "course_id"),
        Index("ix_course_subscriptions_status_expires", "status", "expires_at"),
        Index("ix_course_subscriptions_invoice_status", "invoice_status"),
    )

    def __repr__(self) -> str:
        return f"<CourseSubscription {self.subscription_number} status={self.status}>"


class CourseSessionUsage(Base):
    __tablename__ = "course_session_usages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("course_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_number: Mapped[int] = mapped_column(Integer, nullable=False)
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    booking_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CourseUsageStatus.RESERVED.value)

    # Appointment snapshot (refreshed on reschedule)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    therapist_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    therapist_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_arrangement_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    appointment_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    appointment_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=_utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)  # completed | no_show
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_by_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("booking_id", name="uq_course_session_usages_booking_id"),
        # Session numbers are unique among the non-released usages of a subscription
        Index(
            "uq_course_session_usages_active_number",
            "subscription_id",
            "session_number",
            unique=True,
            postgresql_where=text("status <> 'released'"),
        ),
        CheckConstraint(
            "status IN ('reserved', 'consumed', 'released')",
            name="ck_course_session_usages_status",
        ),
        CheckConstraint("session_number > 0", name="ck_course_session_usages_number_positive"),
        Index("ix_course_session_usages_subscription", "subscription_id"),
    )

    def __repr__(self) -> str:
        return f"<CourseSessionUsage sub={self.subscription_id} #{self.session_number} {self.status}>"

