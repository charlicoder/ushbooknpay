"""Add course_subscriptions and course_session_usages.

Additive: two new tables only. Idempotent (IF NOT EXISTS) because the service
also runs ``Base.metadata.create_all`` on startup.

* ``course_subscriptions`` – a purchased course (session balance, payment,
  invoice, expiry). DB checks keep the balance within ``0..total_sessions``.
* ``course_session_usages`` – one row per redemption booking
  (reserved → consumed | released). ``booking_id`` is unique; session numbers
  are unique among the non-released usages of a subscription.

Revision ID: 0063_course_subscriptions
Revises: 0062_voucher_redemptions
Create Date: 2026-11-06
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0063_course_subscriptions"
down_revision: Union[str, None] = "0062_voucher_redemptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS course_subscriptions (
            id UUID PRIMARY KEY,
            subscription_number VARCHAR(32) NOT NULL UNIQUE,
            customer_id UUID NOT NULL,
            customer_data JSONB,
            course_id UUID NOT NULL,
            course_data JSONB,
            service_id UUID NOT NULL,
            arrangement_type VARCHAR(50) NOT NULL,
            total_sessions INTEGER NOT NULL,
            used_sessions INTEGER NOT NULL DEFAULT 0,
            reserved_sessions INTEGER NOT NULL DEFAULT 0,
            validity_days INTEGER NOT NULL,
            price NUMERIC(10, 3) NOT NULL DEFAULT 0,
            currency VARCHAR(3) NOT NULL DEFAULT 'KWD',
            status VARCHAR(20) NOT NULL DEFAULT 'payment_pending',
            purchased_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            activated_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            payment_option VARCHAR(20),
            payment_id UUID,
            payment_number VARCHAR(32),
            payment_method VARCHAR(50),
            payment_provider VARCHAR(50),
            payment_gateway VARCHAR(50),
            payment_through VARCHAR(20),
            transaction_reference VARCHAR(255),
            payment_link_id UUID,
            payment_url TEXT,
            invoice_id VARCHAR(255),
            invoice_number VARCHAR(100),
            invoice_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            invoice_attempts INTEGER NOT NULL DEFAULT 0,
            invoice_last_error TEXT,
            cancelled_at TIMESTAMPTZ,
            cancel_reason TEXT,
            refund_id UUID,
            refunded_at TIMESTAMPTZ,
            refund_reason TEXT,
            refund_data JSONB,
            sold_by_user VARCHAR(255),
            sold_by_user_data JSONB,
            notes TEXT,
            idempotency_key VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_course_subscriptions_idempotency_key UNIQUE (idempotency_key),
            CONSTRAINT ck_course_subscriptions_total_positive CHECK (total_sessions > 0),
            CONSTRAINT ck_course_subscriptions_used_non_negative CHECK (used_sessions >= 0),
            CONSTRAINT ck_course_subscriptions_reserved_non_negative CHECK (reserved_sessions >= 0),
            CONSTRAINT ck_course_subscriptions_balance
                CHECK (used_sessions + reserved_sessions <= total_sessions),
            CONSTRAINT ck_course_subscriptions_price_non_negative CHECK (price >= 0),
            CONSTRAINT ck_course_subscriptions_status CHECK (status IN (
                'payment_pending', 'payment_failed', 'active', 'exhausted', 'expired',
                'cancelled', 'refunded'
            )),
            CONSTRAINT ck_course_subscriptions_invoice_status
                CHECK (invoice_status IN ('pending', 'created', 'failed'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_course_subscriptions_customer_id ON course_subscriptions (customer_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_course_subscriptions_course_id ON course_subscriptions (course_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_course_subscriptions_status_expires "
        "ON course_subscriptions (status, expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_course_subscriptions_invoice_status ON course_subscriptions (invoice_status)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS course_session_usages (
            id UUID PRIMARY KEY,
            subscription_id UUID NOT NULL REFERENCES course_subscriptions (id) ON DELETE CASCADE,
            session_number INTEGER NOT NULL,
            booking_id UUID NOT NULL,
            booking_number VARCHAR(32),
            status VARCHAR(20) NOT NULL DEFAULT 'reserved',
            branch_id UUID,
            branch_name VARCHAR(255),
            therapist_id UUID,
            therapist_name VARCHAR(255),
            service_arrangement_id UUID,
            service_arrangement_name VARCHAR(255),
            service_name VARCHAR(255),
            appointment_start TIMESTAMPTZ,
            appointment_end TIMESTAMPTZ,
            reserved_at TIMESTAMPTZ,
            consumed_at TIMESTAMPTZ,
            consumed_reason VARCHAR(20),
            released_at TIMESTAMPTZ,
            release_reason TEXT,
            consumed_by_event_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_course_session_usages_booking_id UNIQUE (booking_id),
            CONSTRAINT ck_course_session_usages_status
                CHECK (status IN ('reserved', 'consumed', 'released')),
            CONSTRAINT ck_course_session_usages_number_positive CHECK (session_number > 0)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_course_session_usages_active_number "
        "ON course_session_usages (subscription_id, session_number) WHERE status <> 'released'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_course_session_usages_subscription "
        "ON course_session_usages (subscription_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS course_session_usages")
    op.execute("DROP TABLE IF EXISTS course_subscriptions")

