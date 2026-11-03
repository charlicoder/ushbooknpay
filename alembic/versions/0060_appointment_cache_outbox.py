"""Add appointment_cache_outbox (durable retry queue for ushauth cache writes).

Additive: new table only. Idempotent (IF NOT EXISTS) because the service also
runs ``Base.metadata.create_all`` on startup.

Revision ID: 0060_appointment_cache_outbox
Revises: 0059_booking_sessions
Create Date: 2026-11-02
"""

from __future__ import annotations

from typing import Union

from alembic import op


revision: str = "0060_appointment_cache_outbox"
down_revision: Union[str, None] = "0059_booking_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS appointment_cache_outbox (
            id UUID PRIMARY KEY,
            booking_id VARCHAR(64) NOT NULL,
            path VARCHAR(255) NOT NULL,
            payload JSONB NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 12,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            delivered_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cache_outbox_due "
        "ON appointment_cache_outbox (status, next_attempt_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cache_outbox_booking "
        "ON appointment_cache_outbox (booking_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS appointment_cache_outbox")
