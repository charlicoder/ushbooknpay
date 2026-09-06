"""
app/booking/infrastructure/models.py
──────────────────────────────────────
SQLAlchemy ORM models for the Booking domain.

All monetary fields use Numeric(precision=10, scale=3) to preserve KWD precision.
All primary keys are UUID4.
Audit fields (created_at, updated_at) use server-side defaults.

PostgreSQL-specific features used:
- EXCLUDE USING gist for double-booking prevention on therapist/arrangement slots
- JSONB for flexible metadata storage
- Timezone-aware TIMESTAMPTZ columns
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.booking.domain.value_objects import (
    BookingStatus,
    BookingType,
    PaymentStatus,
    ServiceType,
)
from app.core.database import Base


class Booking(Base):
    """
    Central booking record.

    Preserves all values as they were at booking creation time —
    do not link to mutable external data for financial/historical integrity.
    """

    __tablename__ = "bookings"

    # ── Identity ──────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Customer Reference & Snapshot ─────────────────────────────────────
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    # ── Branch Reference & Snapshot ───────────────────────────────────────
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    # ── Service Reference & Snapshot ──────────────────────────────────────
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)

    # ── Service Arrangement Reference & Snapshot ──────────────────────────
    service_arrangement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    service_arrangement_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    # ── Therapist Reference & Snapshot (Mandatory) ────────────────────────
    therapist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    therapist_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    # ── Add-ons (stored as JSONB list) ────────────────────────────────────
    # Format: [{"addon_id": "uuid", "name": "...", "price": "0.000"}]
    addons: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)

    # ── Appointment Timing ────────────────────────────────────────────────
    appointment_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    appointment_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    appointment_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    extra_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Total booked duration including add-on time (service + extra + addon minutes).
    # Stored explicitly so callers don't need to recompute from parts.
    total_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Service base price at booking time (before arrangement / discount overrides).
    # Stored for reporting and price-change auditing.
    base_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=3), nullable=True, default=None
    )
    # Total duration contributed by all selected add-ons (in minutes).
    addons_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


    # ── Pricing (all Decimal, never float) ───────────────────────────────
    arrangement_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    price_for_extra_minutes: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    addon_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    discount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    tax: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    fees: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False, default=Decimal("0.000")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=3), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KWD")

    # ── Status ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=BookingStatus.REQUESTED.value
    )
    payment_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PaymentStatus.NOT_INITIATED.value
    )

    # ── Booking Type ──────────────────────────────────────────────────────
    booking_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BookingType.BRANCH.value
    )

    # ── Idempotency ───────────────────────────────────────────────────────
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Notes ─────────────────────────────────────────────────────────────
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Payment Details & Meta ────────────────────────────────────────────
    payments_meta: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, server_default=text("'{}'::jsonb")
    )

    # ── Loyalty ───────────────────────────────────────────────────────
    # Populated for booking_type='loyalty' (reward-redeemed bookings).
    # loyalty_data stores a snapshot of the tracker and reward at booking time.
    # reward_id links to the loyalty reward that was redeemed.
    loyalty_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    reward_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )

    # ── Gift Voucher ──────────────────────────────────────────────────────
    # Populated when a gift voucher is used to pay for this booking.
    # voucher_id is a plain UUID (no FK — the voucher lives in gift_vouchers table
    # but we avoid a strict FK here to keep the booking domain self-contained).
    # voucher_data is a snapshot of the voucher at redemption time.
    voucher_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )
    voucher_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # ── Audit ─────────────────────────────────────────────────────────────
    # created_by stores the User UUID (sub) of the API caller — set once at
    # creation and never updated.  Nullable so legacy rows are backward-compat.
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    status_history: Mapped[list["BookingStatusHistory"]] = relationship(
        "BookingStatusHistory",
        back_populates="booking",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ── Indexes ───────────────────────────────────────────────────────────
    __table_args__ = (
        # Fast lookup by customer
        Index("ix_bookings_customer_id", "customer_id"),
        # Fast lookup by branch + date (availability queries)
        Index("ix_bookings_branch_date", "branch_id", "appointment_date"),
        # Fast lookup by therapist + date (availability queries)
        Index("ix_bookings_therapist_date", "therapist_id", "appointment_date"),
        # Fast lookup by status
        Index("ix_bookings_status", "status"),
        # Fast lookup by booking_type
        Index("ix_bookings_booking_type", "booking_type"),
        # Idempotency key uniqueness
        UniqueConstraint("idempotency_key", name="uq_bookings_idempotency_key"),
        # Enforce valid booking_type values
        CheckConstraint("booking_type IN ('home', 'branch', 'loyalty', 'gift_voucher')", name="ck_bookings_booking_type"),
        # Prevent double-booking for therapist using PostgreSQL EXCLUDE
        # NOTE: Requires btree_gist extension. Applied in migration.
        # ExcludeConstraint is defined in the Alembic migration directly
        # to use tstzrange which is not directly expressible in ORM.
    )

    def __repr__(self) -> str:
        return f"<Booking id={self.id} status={self.status} customer={self.customer_id}>"


class BookingStatusHistory(Base):
    """Immutable audit trail for every booking status change."""

    __tablename__ = "booking_status_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # e.g. "customer", "system", "ushnotice"
    changed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    booking: Mapped["Booking"] = relationship(
        "Booking", back_populates="status_history"
    )

    __table_args__ = (
        Index("ix_booking_status_history_booking_id", "booking_id"),
    )


class TemporaryHold(Base):
    """
    Short-lived booking reservation during payment.

    Rows are auto-expired by a background task or PostgreSQL TTL trigger.
    """

    __tablename__ = "temporary_holds"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    therapist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    service_arrangement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    appointment_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    appointment_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_temporary_holds_expires_at", "expires_at"),
        Index("ix_temporary_holds_therapist", "therapist_id", "appointment_start"),
    )
