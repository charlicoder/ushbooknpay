"""Add scheduled-delivery columns to gift_vouchers.

Revision ID: 0058_voucher_scheduled_delivery
Revises: 0057_voucher_update_history
Create Date: 2026-10-30
"""

from __future__ import annotations

from typing import Union

from alembic import op


revision: str = "0058_voucher_scheduled_delivery"
down_revision: Union[str, None] = "0057_voucher_update_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gift_vouchers "
        "ADD COLUMN IF NOT EXISTS delivery_mode VARCHAR(20) NOT NULL DEFAULT 'on_activation'"
    )
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS scheduled_delivery_at TIMESTAMPTZ")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS delivery_schedule_name VARCHAR(128)")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS delivery_schedule_arn VARCHAR(512)")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ")
    op.execute("ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS delivery_result JSONB")
    op.execute(
        "ALTER TABLE gift_vouchers "
        "ADD COLUMN IF NOT EXISTS reschedule_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gift_vouchers_scheduled_due "
        "ON gift_vouchers (scheduled_delivery_at) WHERE status = 'scheduled'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gift_vouchers_scheduled_due")
    for col in (
        "reschedule_count",
        "delivery_result",
        "delivered_at",
        "delivery_schedule_arn",
        "delivery_schedule_name",
        "scheduled_delivery_at",
        "delivery_mode",
    ):
        op.execute(f"ALTER TABLE gift_vouchers DROP COLUMN IF EXISTS {col}")

